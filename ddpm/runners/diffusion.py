import os
import logging
import time
import tqdm
import copy
import pickle 
import numpy as np
import torch

from models.diffusion import Conditional_Model
from models.ema import EMAHelper
from functions import get_optimizer, cycle, create_class_labels
from functions.losses import loss_registry_conditional
from datasets import get_dataset, data_transform, inverse_data_transform, all_but_one_class_path_dataset

import torchvision.utils as tvu
from torchvision.datasets import ImageFolder
from torch.utils.data import DataLoader
import torchvision.transforms as transforms


def torch2hwcuint8(x, clip=False):
    if clip:
        x = torch.clamp(x, -1, 1)
    x = (x + 1.0) / 2.0
    return x


def get_beta_schedule(beta_schedule, *, beta_start, beta_end, num_diffusion_timesteps):
    def sigmoid(x):
        return 1 / (np.exp(-x) + 1)

    if beta_schedule == "quad":
        betas = (
            np.linspace(
                beta_start ** 0.5,
                beta_end ** 0.5,
                num_diffusion_timesteps,
                dtype=np.float64,
            )
            ** 2
        )
    elif beta_schedule == "linear":
        betas = np.linspace(
            beta_start, beta_end, num_diffusion_timesteps, dtype=np.float64
        )
    elif beta_schedule == "const":
        betas = beta_end * np.ones(num_diffusion_timesteps, dtype=np.float64)
    elif beta_schedule == "jsd":  # 1/T, 1/(T-1), 1/(T-2), ..., 1
        betas = 1.0 / np.linspace(
            num_diffusion_timesteps, 1, num_diffusion_timesteps, dtype=np.float64
        )
    elif beta_schedule == "sigmoid":
        betas = np.linspace(-6, 6, num_diffusion_timesteps)
        betas = sigmoid(betas) * (beta_end - beta_start) + beta_start
    else:
        raise NotImplementedError(beta_schedule)
    assert betas.shape == (num_diffusion_timesteps,)
    return betas


class Diffusion(object):
    def __init__(self, args, config):
        self.args = args
        self.config = config#初始化
        
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')#调用GPU

        self.model_var_type = config.model.var_type
        betas = get_beta_schedule(
            beta_schedule=config.diffusion.beta_schedule,
            beta_start=config.diffusion.beta_start,
            beta_end=config.diffusion.beta_end,
            num_diffusion_timesteps=config.diffusion.num_diffusion_timesteps,
        )
        betas = self.betas = torch.from_numpy(betas).float().to(self.device)
        self.num_timesteps = betas.shape[0]#确定模型变量类型，这里是fixedlarge

        alphas = 1.0 - betas#计算扩散过程中的参数
        alphas_cumprod = alphas.cumprod(dim=0)
        alphas_cumprod_prev = torch.cat(
            [torch.ones(1).to(self.device), alphas_cumprod[:-1]], dim=0
        )
        posterior_variance = (
            betas * (1.0 - alphas_cumprod_prev) / (1.0 - alphas_cumprod)
        )#计算后验方差
        if self.model_var_type == "fixedlarge":#根据模型设置对数方差
            self.logvar = betas.log()
            # torch.cat(
            # [posterior_variance[1:2], betas[1:]], dim=0).log()
        elif self.model_var_type == "fixedsmall":
            self.logvar = posterior_variance.clamp(min=1e-20).log()
            
    def save_fim(self):
        
        args, config = self.args, self.config
        bs = torch.cuda.device_count() # process 1 sample per GPU, so bs == number of gpus
        fim_dataset = ImageFolder(os.path.join(args.ckpt_folder, "class_samples"), 
                                  transform=transforms.ToTensor())
        fim_loader = DataLoader(fim_dataset, batch_size=bs, 
                                num_workers=config.data.num_workers, shuffle=True)

        print("Loading checkpoints {}".format(args.ckpt_folder))
        model = Conditional_Model(self.config)
        states = torch.load(
            os.path.join(self.args.ckpt_folder, "ckpts/ckpt.pth"),
            map_location=self.device,
        )
        model = model.to(self.device)#加载模型至计算机上
        model = torch.nn.DataParallel(model)#在多个GPU上进行计算
        model.load_state_dict(states[0], strict=True)#加载模型的预训练权重
        model.eval()#设置为评估模式
    
        # calculate FIM 
        fisher_dict = {}
        fisher_dict_temp_list = [{} for _ in range(bs)]

        for name, param in model.named_parameters():
            fisher_dict[name] = param.data.clone().zero_()
            
            for i in range(bs):
                fisher_dict_temp_list[i][name] = param.data.clone().zero_()
        
        # calculate Fisher information diagonals
        for step, data in enumerate(tqdm.tqdm(fim_loader, desc="Calculating Fisher information matrix")):
            
            x, c = data
            x, c = x.to(self.device), c.to(self.device)
        
            b = self.betas
            ts = torch.chunk(torch.arange(0, self.num_timesteps), args.n_chunks)#将时间分为多个块，并遍历每个块
            
            for _t in ts:
                for i in range(len(_t)):
                    e = torch.randn_like(x)
                    t = torch.tensor([_t[i]]).expand(bs).to(self.device)
                    
                    # keepdim=True will return loss of shape [bs], so gradients across batch are NOT averaged yet
                    if i == 0:
                        loss = loss_registry_conditional[config.model.type](model, x, t, c, e, b, keepdim=True)
                    else:
                        loss += loss_registry_conditional[config.model.type](model, x, t, c, e, b, keepdim=True)

                # store first-order gradients for each sample separately in temp dictionary
                # for each timestep chunk
                for i in range(bs):
                    model.zero_grad()
                    if i != len(loss) - 1:
                        loss[i].backward(retain_graph=True)
                    else:
                        loss[i].backward()
                    for name, param in model.named_parameters():
                        fisher_dict_temp_list[i][name] += param.grad.data
                del loss
            
            # after looping through all 1000 time steps, we can now aggregrate each individual sample's gradient and square and average
            for name, param in model.named_parameters():
                for i in range(bs):
                    fisher_dict[name].data += (fisher_dict_temp_list[i][name].data ** 2) / len(fim_loader.dataset)
                    fisher_dict_temp_list[i][name] = fisher_dict_temp_list[i][name].clone().zero_()

            if (step+1) % config.training.save_freq == 0:
                with open(os.path.join(args.ckpt_folder,'fisher_dict.pkl'), 'wb') as f:
                    pickle.dump(fisher_dict, f)
                    
        # save at the end
        with open(os.path.join(args.ckpt_folder,'fisher_dict.pkl'), 'wb') as f:
            pickle.dump(fisher_dict, f)

    def train(self):
        args, config = self.args, self.config
        D_train_loader = get_dataset(args, config)
        D_train_iter = cycle(D_train_loader)
        
        model = Conditional_Model(config)

        optimizer = get_optimizer(self.config, model.parameters())
        model.to(self.device)
        model = torch.nn.DataParallel(model)
        
        if self.config.model.ema:
            ema_helper = EMAHelper(mu=self.config.model.ema_rate)
            ema_helper.register(model)
        else:
            ema_helper = None
        
        model.train()
        
        start = time.time()
        for step in range(0, self.config.training.n_iters):

            model.train()
            x, c = next(D_train_iter)
            n = x.size(0)
            x = x.to(self.device)
            x = data_transform(self.config, x)
            e = torch.randn_like(x)#生成随机高斯噪声
            b = self.betas

            # antithetic sampling
            t = torch.randint(
                low=0, high=self.num_timesteps, size=(n // 2 + 1,)
            ).to(self.device)
            t = torch.cat([t, self.num_timesteps - t - 1], dim=0)[:n]
            loss = loss_registry_conditional[config.model.type](model, x, t, c, e, b)
            
            if (step+1) % self.config.training.log_freq  == 0:
                end = time.time()
                logging.info(
                    f"step: {step}, loss: {loss.item()}, time: {end-start}"
                )
                start = time.time()
                
            optimizer.zero_grad()
            loss.backward()

            try:#进行梯度裁剪
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), config.optim.grad_clip
                )
            except Exception:
                pass
            optimizer.step()

            if self.config.model.ema:
                ema_helper.update(model)

            if (step+1) % self.config.training.snapshot_freq == 0:
                states = [
                    model.state_dict(),
                    optimizer.state_dict(),
                    step,
                ]
                if self.config.model.ema:
                    states.append(ema_helper.state_dict())

                torch.save(
                    states,
                    os.path.join(self.config.ckpt_dir, "ckpt.pth"),
                )
                #torch.save(states, os.path.join(self.config.ckpt_dir, "ckpt_latest.pth"))

                test_model = ema_helper.ema_copy(model) if self.config.model.ema else copy.deepcopy(model)
                test_model.eval()
                self.sample_visualization(test_model, step, args.cond_scale)
                del test_model
                
 
    def train_forget(self):
        
        args, config = self.args, self.config
        logging.info(f"Training diffusion forget with contrastive and EWC. Gamma: {config.training.gamma}, lambda: {config.training.lmbda}")
        D_train_loader = all_but_one_class_path_dataset(config, os.path.join(args.ckpt_folder, "class_samples"), args.label_to_forget)
        D_train_iter = cycle(D_train_loader)#加载除了要忘记类别之外的所有类别的dataloader
        
        print("Loading checkpoints {}".format(args.ckpt_folder))
        model = Conditional_Model(config)
        states = torch.load(
            os.path.join(args.ckpt_folder, "ckpts/ckpt.pth"),
            map_location=self.device,
        )
        model = model.to(self.device)
        model = torch.nn.DataParallel(model)
        model.load_state_dict(states[0], strict=True)
        optimizer = get_optimizer(config, model.parameters())
        
        if self.config.model.ema:
            ema_helper = EMAHelper(mu=config.model.ema_rate)
            ema_helper.register(model)
            ema_helper.load_state_dict(states[-1])
            #model = ema_helper.ema_copy(model_no_ema)
        else:
            ema_helper = None

        with open(os.path.join(args.ckpt_folder, 'fisher_dict.pkl'), 'rb') as f:
            fisher_dict = pickle.load(f)
        
        params_mle_dict = {}
        for name, param in model.named_parameters():
            params_mle_dict[name] = param.data.clone()
        
        label_choices = list(range(config.data.n_classes))
        label_choices.remove(args.label_to_forget)
        
        for step in range(0, config.training.n_iters):
            
            model.train()
            x_remember, c_remember = next(D_train_iter)
            x_remember, c_remember = x_remember.to(self.device), c_remember.to(self.device)
            x_remember = data_transform(config, x_remember)
            
            n = x_remember.size(0)
            channels = config.data.channels
            img_size = config.data.image_size
            c_forget = (torch.ones(n, dtype=int) * args.label_to_forget).to(self.device)
            x_forget = ( torch.rand((n, channels, img_size, img_size), device=self.device) - 0.5 ) * 2.
            e_remember = torch.randn_like(x_remember)
            e_forget = torch.randn_like(x_forget)
            b = self.betas
            
            # antithetic sampling
            t = torch.randint(
                low=0, high=self.num_timesteps, size=(n // 2 + 1,)
            ).to(self.device)
            t = torch.cat([t, self.num_timesteps - t - 1], dim=0)[:n]
            loss = loss_registry_conditional[config.model.type](model, x_forget, t, c_forget, e_forget, b, cond_drop_prob = 0.) + \
                   config.training.gamma * loss_registry_conditional[config.model.type](model, x_remember, t, c_remember, e_remember, b, cond_drop_prob = 0.)   
            forgetting_loss = loss.item()
            
            ewc_loss = 0.
            for name, param in model.named_parameters():
                _loss = fisher_dict[name].to(self.device) * (param - params_mle_dict[name].to(self.device)) ** 2
                loss += config.training.lmbda * _loss.sum()
                ewc_loss += config.training.lmbda * _loss.sum()

            if (step+1) % config.training.log_freq == 0:
                logging.info(
                    f"step: {step}, loss: {loss.item()}, forgetting loss: {forgetting_loss}, ewc loss: {ewc_loss}"
                )

            optimizer.zero_grad()
            loss.backward()

            try:
                torch.nn.utils.clip_grad_norm_(
                    model.parameters(), config.optim.grad_clip
                )
            except Exception:
                pass
            
            optimizer.step()

            if self.config.model.ema:
                ema_helper.update(model)

            if (step+1) % config.training.snapshot_freq == 0:
                states = [
                    model.state_dict(),
                    optimizer.state_dict(),
                    #epoch,
                    step,
                ]
                if config.model.ema:
                    states.append(ema_helper.state_dict())

                torch.save(
                    states,
                    os.path.join(config.ckpt_dir, "ckpt.pth"),
                )
                #torch.save(states, os.path.join(self.config.ckpt_dir, "ckpt_latest.pth"))

                test_model = ema_helper.ema_copy(model) if config.model.ema else copy.deepcopy(model)
                test_model.eval()
                self.sample_visualization(test_model, step, args.cond_scale)
                del test_model

    def load_ema_model(self):
        model = Conditional_Model(self.config)
        states = torch.load(
            os.path.join(self.args.ckpt_folder, "ckpts/ckpt.pth"),
            map_location=self.device,
        )
        model = model.to(self.device)
        model = torch.nn.DataParallel(model)
        model.load_state_dict(states[0], strict=True)
        
        if self.config.model.ema:
            ema_helper = EMAHelper(mu=self.config.model.ema_rate)
            ema_helper.register(model)
            ema_helper.load_state_dict(states[-1])
            test_model = ema_helper.ema_copy(model)
        else:
            ema_helper = None

        model.eval()
        return model
    
    def sample(self):
        model = Conditional_Model(self.config)
        states = torch.load(
            os.path.join(self.args.ckpt_folder, "ckpts/ckpt.pth"),
            map_location=self.device,
        )
        model = model.to(self.device)
        model = torch.nn.DataParallel(model)
        model.load_state_dict(states[0], strict=True)
        
        if self.config.model.ema:
            ema_helper = EMAHelper(mu=self.config.model.ema_rate)
            ema_helper.register(model)
            ema_helper.load_state_dict(states[-1])
            test_model = ema_helper.ema_copy(model)
        else:
            ema_helper = None

        model.eval()
        
        if self.args.mode == 'sample_fid':
            self.sample_fid(test_model, self.args.cond_scale)
        elif self.args.mode == 'sample_classes':
            self.sample_classes(test_model, self.args.cond_scale)
        # elif self.args.mode == 'gen_data':
        #     self.sample_gen_data(test_model, self.args.cond_scale)
        elif self.args.mode == 'visualization':
            self.sample_visualization(test_model, str(self.args.cond_scale), self.args.cond_scale)
        # elif self.args.mode == 'one_class':
        #     self.sample_one_class(test_model, self.args.cond_scale, self.args.class_label)
    

    def sample_classes(self, model, cond_scale):
        """
        Samples each class from the model. Can be used to calculate FIM, for generative replay 
        or for classifier evaluation. Stores samples in "./class_samples/<class_label>".
        """
        config = self.config
        args = self.args
        sample_dir = os.path.join(args.ckpt_folder, "class_samples")
        os.makedirs(sample_dir, exist_ok=True)
        img_id = 0
        # total_n_samples = 5000
        # assert total_n_samples % config.data.n_classes == 0
        classes, _ = create_class_labels(args.classes_to_generate, n_classes=config.data.n_classes)
        n_samples_per_class = args.n_samples_per_class
        
        for i in classes:
            
            os.makedirs(os.path.join(sample_dir, str(i)), exist_ok=True)
            if n_samples_per_class % config.sampling.batch_size == 0:
                n_rounds = n_samples_per_class // config.sampling.batch_size 
            else:
                n_rounds = n_samples_per_class // config.sampling.batch_size  + 1 
            n_left = n_samples_per_class # tracker on how many samples left to generate
            
            with torch.no_grad():
                for j in tqdm.tqdm(
                    range(n_rounds), desc=f"Generating image samples for class {i} to use as dataset"
                ):
                    if n_left >= config.sampling.batch_size:
                        n = config.sampling.batch_size
                    else:
                        n = n_left
                    
                    x = torch.randn(
                        n,
                        config.data.channels,
                        config.data.image_size,
                        config.data.image_size,
                        device=self.device,
                    )
                    c = torch.ones(x.size(0), device=self.device, dtype=int) * int(i)
                    x = self.sample_image(x, model, c, cond_scale)
                    x = inverse_data_transform(config, x)#还原成原始格式
                    
                    for k in range(n):
                        tvu.save_image(x[k], os.path.join(sample_dir, str(c[k].item()), f"{img_id}.png"), normalize=True)
                        img_id += 1
                    
                    n_left -= n
                    

    def sample_one_class(self, model, cond_scale, class_label):
        """
        Samples one class only for classifier evaluation.
        """
        config = self.config
        args = self.args
        sample_dir = os.path.join(args.ckpt_folder, "class_" + str(class_label))
        os.makedirs(sample_dir, exist_ok=True)
        img_id = 0
        total_n_samples = 500
            
        if total_n_samples % config.sampling.batch_size == 0:
            n_rounds = total_n_samples // config.sampling.batch_size 
        else:
            n_rounds = total_n_samples // config.sampling.batch_size  + 1 
        n_left = total_n_samples # tracker on how many samples left to generate
        
        with torch.no_grad():
            for j in tqdm.tqdm(
                range(n_rounds), desc=f"Generating image samples for class {class_label}"
            ):
                if n_left >= config.sampling.batch_size:
                    n = config.sampling.batch_size
                else:
                    n = n_left
                
                x = torch.randn(
                    n,
                    config.data.channels,
                    config.data.image_size,
                    config.data.image_size,
                    device=self.device,
                )
                c = torch.ones(x.size(0), device=self.device, dtype=int) * class_label
                x = self.sample_image(x, model, c, cond_scale)
                x = inverse_data_transform(config, x)
                
                for k in range(n):
                    tvu.save_image(x[k], os.path.join(sample_dir, f"{img_id}.png"), normalize=True)
                    img_id += 1
                
                n_left -= n
                
    def sample_fid(self, model, cond_scale):
        config = self.config
        args = self.args
        img_id = 0
        # total_n_samples = 45000
        # assert total_n_samples % (config.data.n_classes - 1) == 0
        # n_samples_per_class = total_n_samples // (config.data.n_classes-1)
        
        classes, excluded_classes = create_class_labels(args.classes_to_generate, n_classes=config.data.n_classes)
        n_samples_per_class = args.n_samples_per_class
        # classes = list(range(config.data.n_classes))
        # classes.remove(args.label_to_forget)
        
        sample_dir = f"fid_samples_guidance_{args.cond_scale}"
        if excluded_classes:
            excluded_classes_str = "_".join(str(i) for i in excluded_classes)
            sample_dir = f"{sample_dir}_excluded_class_{excluded_classes_str}"
        sample_dir = os.path.join(args.ckpt_folder, sample_dir)
        os.makedirs(sample_dir, exist_ok=True)
        
        for i in classes:
            
            if n_samples_per_class % config.sampling.batch_size == 0:
                n_rounds = n_samples_per_class // config.sampling.batch_size 
            else:
                n_rounds = n_samples_per_class // config.sampling.batch_size  + 1 
            n_left = n_samples_per_class # tracker on how many samples left to generate
            
            with torch.no_grad():
                for j in tqdm.tqdm(
                    range(n_rounds), desc=f"Generating image samples for class {i} for FID"
                ):
                    if n_left >= config.sampling.batch_size:
                        n = config.sampling.batch_size
                    else:
                        n = n_left
                    
                    x = torch.randn(
                        n,
                        config.data.channels,
                        config.data.image_size,
                        config.data.image_size,
                        device=self.device,
                    )
                    c = torch.ones(x.size(0), device=self.device, dtype=int) * int(i)
                    x = self.sample_image(x, model, c, cond_scale)
                    x = inverse_data_transform(config, x)
                    
                    for k in range(n):
                        tvu.save_image(x[k], os.path.join(sample_dir, f"{img_id}.png"), normalize=True)
                        img_id += 1
                    
                    n_left -= n

    def sample_image(self, x, model, c, cond_scale, last=True):
        try:
            skip = self.args.skip
        except Exception:
            skip = 1

        if self.args.sample_type == "generalized":
            if self.args.skip_type == "uniform":
                skip = self.num_timesteps // self.args.timesteps
                seq = range(0, self.num_timesteps, skip)
            elif self.args.skip_type == "quad":
                seq = (
                    np.linspace(
                        0, np.sqrt(self.num_timesteps * 0.8), self.args.timesteps
                    )
                    ** 2
                )
                seq = [int(s) for s in list(seq)]
            else:
                raise NotImplementedError
            from functions.denoising import generalized_steps_conditional

            xs = generalized_steps_conditional(x, c, seq, model, self.betas, cond_scale, eta=self.args.eta)
            x = xs
        elif self.args.sample_type == "ddpm_noisy":
            if self.args.skip_type == "uniform":
                skip = self.num_timesteps // self.args.timesteps
                seq = range(0, self.num_timesteps, skip)
            elif self.args.skip_type == "quad":
                seq = (
                    np.linspace(
                        0, np.sqrt(self.num_timesteps * 0.8), self.args.timesteps
                    )
                    ** 2
                )
                seq = [int(s) for s in list(seq)]
            else:
                raise NotImplementedError
            from functions.denoising import ddpm_steps_conditional

            x = ddpm_steps_conditional(x, c, seq, model, self.betas)
        else:
            raise NotImplementedError
        if last:
            x = x[0][-1]
        return x

    def sample_visualization(self, model, name, cond_scale):
        config = self.config
        total_n_samples = config.training.visualization_samples
        assert total_n_samples % config.data.n_classes == 0
        n_rounds = total_n_samples // config.sampling.batch_size if config.sampling.batch_size < total_n_samples else 1
        c = torch.repeat_interleave(torch.arange(config.data.n_classes), total_n_samples//config.data.n_classes)
        c_chunks = torch.chunk(c, n_rounds, dim=0)
        
        with torch.no_grad():
            all_imgs = []
            for i in tqdm.tqdm(
                range(n_rounds), desc="Generating image samples for visualization."
            ):
                c = c_chunks[i]
                n = c.size(0)
                x = torch.randn(
                    n,
                    config.data.channels,
                    config.data.image_size,
                    config.data.image_size,
                    device=self.device,
                )
                
                x = self.sample_image(x, model, c, cond_scale)
                x = inverse_data_transform(config, x)

                all_imgs.append(x)
            
            all_imgs = torch.cat(all_imgs)
            grid = tvu.make_grid(all_imgs, nrow=total_n_samples//config.data.n_classes, normalize=True, padding=0)
            
            try:
                tvu.save_image(grid, os.path.join(self.config.log_dir, f'sample-{name}.png')) # if called during training of base model
            except AttributeError:
                tvu.save_image(grid, os.path.join(self.args.ckpt_folder, f'sample-{name}.png')) # if called from sample.py

