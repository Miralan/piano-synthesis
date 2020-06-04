from dataset import MAESTRO
from torch.utils.data import Dataset, DataLoader
from model_nms_latent import *
from nnAudio import Spectrogram
import torch
from torch.distributions import kl_divergence, Normal
from torch import optim
from sklearn.metrics import accuracy_score
from tensorboardX import SummaryWriter
import json, os
import datetime
from tqdm import tqdm
import numpy as np
import matplotlib.pyplot as plt
import librosa.display
from collections import Counter


class Normalizer():
    """This class is for normalizing the spectrograms batch by batch. The normalization used is min-max, two modes 'framewise' and 'imagewise' can be selected. In this paper, we found that 'imagewise' normalization works better than 'framewise'"""
    def __init__(self, mode='framewise'):

        self.min_max_mean = None
        if mode == 'framewise':
            def normalize(x):
                size = x.shape
                x_max = x.max(1, keepdim=True)[0] # Finding max values for each frame
                x_min = x.min(1, keepdim=True)[0]  
                output = (x-x_min)/(x_max-x_min) # If there is a column with all zero, nan will occur
                output[torch.isnan(output)]=0 # Making nan to 0
                return output
        elif mode == 'imagewise':
            def normalize(x):
                size = x.shape
                # x_max = x.view(size[0], size[1]*size[2]).max(1, keepdim=True)[0]
                # x_min = x.view(size[0], size[1]*size[2]).min(1, keepdim=True)[0]
                # fix constant min max to be used
                x_max = torch.Tensor([[10]]).cuda()
                x_min = torch.Tensor([[-20]]).cuda()

                if self.min_max_mean is None:
                    self.min_max_mean = (x_min.mean(), x_max.mean())
                else:
                    self.min_max_mean = ((self.min_max_mean[0] + x_min.mean()) / 2, \
                                        (self.min_max_mean[1] + x_max.mean()) / 2)
                
                x_max = x_max.unsqueeze(1) # Make it broadcastable
                x_min = x_min.unsqueeze(1) # Make it broadcastable 
                return (x-x_min)/(x_max-x_min+1e-15)
        else:
            print('please choose the correct mode')
        self.normalize = normalize

    def transform(self, x):
        return self.normalize(x)
    
    def save_minmax(self):
        torch.save(self.min_max_mean, "normalizer.pt")


def loss_function(melspec_hat, melspec, z_art_lst, art_cls_lst, mu_art_lst, var_art_lst, 
                z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst,
                z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob,
                mu_art_lst_hat, var_art_lst_hat, 
                mu_dyn_lst_hat, var_dyn_lst_hat,
                art_cls_lst_hat, dyn_cls_lst_hat,
                is_sup=False, emotion_cls=None, step=None, beta=1):
    
    # kl annealing
    if not step is None:
        beta_0 = min(step / 20000 * beta, beta)
    else:
        beta_0 = 1
    beta_1 = min(step / 50000 * beta, beta)
    
    # reconstruction loss
    recon_loss = torch.nn.MSELoss()(melspec_hat, melspec)

    # handle articulation
    cls_art_loss = torch.nn.CrossEntropyLoss()(art_cls_lst.view(-1, 2), emotion_cls[0].cuda().long().view(-1))
    clf_art_acc = accuracy_score(torch.argmax(art_cls_lst, dim=-1).cpu().detach().numpy().reshape(-1),
                                emotion_cls[0].cpu().detach().numpy().reshape(-1))

    # kl loss
    kl_loss_art, kl_loss_dyn = 0, 0
    for i in range(mu_art_lst.shape[1]):
        mu, var = model.mu_art_lookup(emotion_cls[0][:, i].cuda().long()), \
                        model.logvar_art_lookup(emotion_cls[0][:, i].cuda().long()).exp_()
        dis = Normal(mu, var)
        dis_art = Normal(mu_art_lst[:, i, :], var_art_lst[:, i, :])
        kl_loss_art += kl_divergence(dis_art, dis).mean()

        mu, var = model.mu_dyn_lookup(emotion_cls[1][:, i].cuda().long()), \
                        model.logvar_dyn_lookup(emotion_cls[1][:, i].cuda().long()).exp_()
        dis = Normal(mu, var)
        dis_dyn = Normal(mu_dyn_lst[:, i, :], var_dyn_lst[:, i, :])
        kl_loss_dyn += kl_divergence(dis_dyn, dis).mean()

    kl_loss_art = kl_loss_art / mu_art_lst.shape[1]
    kl_loss_dyn = kl_loss_dyn / mu_dyn_lst.shape[1]
    
    # handle dynamic
    cls_dyn_loss = torch.nn.CrossEntropyLoss()(dyn_cls_lst.view(-1, 2), emotion_cls[1].cuda().long().view(-1))        
    clf_dyn_acc = accuracy_score(torch.argmax(dyn_cls_lst, dim=-1).cpu().detach().numpy().reshape(-1),
                                emotion_cls[1].cpu().detach().numpy().reshape(-1))

    # handle style -- unsupervised
    kl_style_loss = 0
    for k in torch.arange(0, 4):       # number of components
        mu, var = model.mu_style_lookup(k.cuda()), model.logvar_style_lookup(k.cuda()).exp_()
        dis = Normal(mu, var)
        kld_lat = torch.mean(kl_divergence(z_style_dist, dis), dim=-1)
        kld_lat *= cls_z_style_prob[:, k]
        kl_style_loss += kld_lat.mean()
    
    def entropy(qy_x, logLogit_qy_x):
        return torch.mean(qy_x * torch.nn.functional.log_softmax(logLogit_qy_x, dim=1), dim=1)

    kl_style_cls = (entropy(cls_z_style_prob, cls_z_style_logits) - np.log(1 / 4)).mean()
    h_q_style = entropy(cls_z_style_prob, cls_z_style_logits).mean()

    # handle articulation and dynamic hat
    cls_art_hat_loss = torch.nn.CrossEntropyLoss()(art_cls_lst_hat.view(-1, 2), emotion_cls[0].cuda().long().view(-1))
    clf_art_hat_acc = accuracy_score(torch.argmax(art_cls_lst_hat, dim=-1).cpu().detach().numpy().reshape(-1),
                                emotion_cls[0].cpu().detach().numpy().reshape(-1))

    cls_dyn_hat_loss = torch.nn.CrossEntropyLoss()(dyn_cls_lst_hat.view(-1, 2), emotion_cls[1].cuda().long().view(-1))        
    clf_dyn_hat_acc = accuracy_score(torch.argmax(dyn_cls_lst_hat, dim=-1).cpu().detach().numpy().reshape(-1),
                                emotion_cls[1].cpu().detach().numpy().reshape(-1))

    kl_loss_art_hat, kl_loss_dyn_hat = 0, 0
    for i in range(mu_art_lst_hat.shape[1]):
        mu, var = model.mu_art_lookup(emotion_cls[0][:, i].cuda().long()), \
                        model.logvar_art_lookup(emotion_cls[0][:, i].cuda().long()).exp_()
        dis = Normal(mu, var)
        dis_art = Normal(mu_art_lst_hat[:, i, :], var_art_lst_hat[:, i, :])
        kl_loss_art_hat += kl_divergence(dis_art, dis).mean()

        mu, var = model.mu_dyn_lookup(emotion_cls[1][:, i].cuda().long()), \
                        model.logvar_dyn_lookup(emotion_cls[1][:, i].cuda().long()).exp_()
        dis = Normal(mu, var)
        dis_dyn = Normal(mu_dyn_lst_hat[:, i, :], var_dyn_lst_hat[:, i, :])
        kl_loss_dyn_hat += kl_divergence(dis_dyn, dis).mean()

    kl_loss_art_hat = kl_loss_art_hat / mu_art_lst_hat.shape[1]
    kl_loss_dyn_hat = kl_loss_dyn_hat / mu_dyn_lst_hat.shape[1]
    
    # consolidate
    cls_loss = 2 * cls_art_loss + cls_dyn_loss
    cls_loss_hat = 2 * cls_art_hat_loss + cls_dyn_hat_loss
    kl_loss = kl_loss_art + kl_loss_dyn
    kl_style = kl_style_loss + kl_style_cls
    kl_loss_hat = kl_loss_art_hat + kl_loss_dyn_hat

    loss = 10 * recon_loss + cls_loss + cls_loss_hat + beta_0 * (kl_loss + kl_style + kl_loss_hat)

    return loss, recon_loss, kl_loss, cls_art_loss, clf_art_acc, cls_dyn_loss, clf_dyn_acc, \
            kl_loss_hat, cls_art_hat_loss, clf_art_hat_acc, cls_dyn_hat_loss, clf_dyn_hat_acc, \
            kl_style_loss, h_q_style


def training():
    step_unsup, step_sup = 0, 0
    learning_rate_counter = 0
    total_epoch = args['epochs']
    lr_factor = 2

    for ep in range(1, total_epoch):
        print("Epoch: {} / {}".format(ep, total_epoch))

        print("Supervised...")
        # train supervised
        for i, x in enumerate(train_s_dl):
            
            optimizer.zero_grad()

            audio, onset_pr, frame_pr, emotion_cls = x     # (b, 320000), (b, t=625, 88)
            # pr = torch.cat([onset_pr, frame_pr], dim=-1)
            pr = onset_pr
            melspec = torch.transpose(wav_to_melspec(audio), 1, 2)[:, :-1, :]   # (b, 625, 128)

            # use log melspec
            pr = pr.cuda()
            # emotion_cls = emotion_cls.cuda()
            if args["melspec_mode"] == "log":
                melspec = torch.log(melspec + 1e-12).cuda()
            elif args["melspec_mode"] == "log-tanh":
                melspec = torch.nn.Tanh()(0.25 * torch.log(melspec + 1e-12)).cuda()
            elif args["melspec_mode"] == "log-minmax":
                melspec = normalizer.transform(torch.log(melspec + 1e-12)).cuda()

            melspec_hat, z_art_lst, art_cls_lst, mu_art_lst, var_art_lst, \
                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst, \
                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob, \
                    mu_art_lst_hat, var_art_lst_hat, mu_dyn_lst_hat, var_dyn_lst_hat, \
                    art_cls_lst_hat, dyn_cls_lst_hat = model(melspec, pr)
            
            loss, recon_loss, kl_loss, cls_art_loss, clf_art_acc, cls_dyn_loss, clf_dyn_acc, \
                kl_loss_hat, cls_art_hat_loss, clf_art_hat_acc, cls_dyn_hat_loss, clf_dyn_hat_acc, \
                kl_style_loss, h_q_style = loss_function(melspec_hat, melspec, 
                                                    z_art_lst, art_cls_lst, mu_art_lst, var_art_lst,
                                                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst,
                                                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob,
                                                    mu_art_lst_hat, var_art_lst_hat, 
                                                    mu_dyn_lst_hat, var_dyn_lst_hat,
                                                    art_cls_lst_hat, dyn_cls_lst_hat,
                                                    step=step_sup,
                                                    is_sup=True, emotion_cls=emotion_cls)

            loss.backward()
            optimizer.step()
                
            print("", end="\r")
            print('''Batch {}/{}: Recon: {:.4} | CLF Art Loss: {:.4} | Acc Art: {:.4} | CLF Dyn Loss: {:.4} | Acc Dyn: {:.4} | CLF Art Hat Loss: {:.4} | Acc Art Hat: {:.4} | CLF Dyn Hat Loss: {:.4} | Acc Dyn Hat: {:.4}'''.format(i+1, len(train_s_dl),
                    recon_loss.item(), cls_art_loss.item(), clf_art_acc, cls_dyn_loss.item(), clf_dyn_acc,
                    cls_art_hat_loss.item(), clf_art_hat_acc, cls_dyn_hat_loss.item(), clf_dyn_hat_acc
                    ), end="\r")
                              
            train_sup_writer.add_scalar('Recon', recon_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('KL Sup', kl_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('KL Hat Sup', kl_loss_hat.item(), global_step=step_sup)
            train_sup_writer.add_scalar('KL Style', kl_style_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('Entropy', h_q_style.item(), global_step=step_sup)
            train_sup_writer.add_scalar('CLF Art Loss', cls_art_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('CLF Art Acc', clf_art_acc, global_step=step_sup)
            train_sup_writer.add_scalar('CLF Dyn Loss', cls_dyn_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('CLF Dyn Acc', clf_dyn_acc, global_step=step_sup)
            train_sup_writer.add_scalar('CLF Art Hat Loss', cls_art_hat_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('CLF Art Hat Acc', clf_art_hat_acc, global_step=step_sup)
            train_sup_writer.add_scalar('CLF Dyn Hat Loss', cls_dyn_hat_loss.item(), global_step=step_sup)
            train_sup_writer.add_scalar('CLF Dyn Hat Acc', clf_dyn_hat_acc, global_step=step_sup)

            step_sup += 1
            learning_rate_counter +=1
        
        # evaluate supervised
        eval_loss, eval_recon_loss, eval_cls_art_loss, eval_cls_art_acc = 0, 0, 0, 0
        eval_cls_dyn_loss, eval_cls_dyn_acc, eval_cls_art_hat_loss, eval_cls_art_hat_acc = 0, 0, 0, 0
        eval_cls_dyn_hat_loss, eval_cls_dyn_hat_acc, eval_kl_style_loss, eval_entropy_style = 0, 0, 0, 0

        for i, x in enumerate(val_s_dl):
            
            audio, onset_pr, frame_pr, emotion_cls = x     # (b, 320000), (b, t=625, 88)
            # pr = torch.cat([onset_pr, frame_pr], dim=-1)
            pr = onset_pr
            melspec = torch.transpose(wav_to_melspec(audio), 1, 2)[:, :-1, :]   # (b, 625, 128)
            
            # use log melspec
            pr = pr.cuda()
            # emotion_cls = emotion_cls.cuda()
            if args["melspec_mode"] == "log":
                melspec = torch.log(melspec + 1e-12).cuda()
            elif args["melspec_mode"] == "log-tanh":
                melspec = torch.nn.Tanh()(0.25 * torch.log(melspec + 1e-12)).cuda()
            elif args["melspec_mode"] == "log-minmax":
                melspec = normalizer.transform(torch.log(melspec + 1e-12)).cuda()
            
            melspec_hat, z_art_lst, art_cls_lst, mu_art_lst, var_art_lst, \
                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst, \
                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob, \
                    mu_art_lst_hat, var_art_lst_hat, mu_dyn_lst_hat, var_dyn_lst_hat, \
                    art_cls_lst_hat, dyn_cls_lst_hat = model(melspec, pr)
            
            loss, recon_loss, kl_loss, cls_art_loss, clf_art_acc, cls_dyn_loss, clf_dyn_acc, \
                kl_loss_hat, cls_art_hat_loss, clf_art_hat_acc, cls_dyn_hat_loss, clf_dyn_hat_acc, \
                kl_style_loss, h_q_style = loss_function(melspec_hat, melspec, 
                                                    z_art_lst, art_cls_lst, mu_art_lst, var_art_lst,
                                                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst,
                                                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob,
                                                    mu_art_lst_hat, var_art_lst_hat, 
                                                    mu_dyn_lst_hat, var_dyn_lst_hat,
                                                    art_cls_lst_hat, dyn_cls_lst_hat,
                                                    step=step_sup,
                                                    is_sup=True, emotion_cls=emotion_cls)
            
            eval_loss += loss.item() / len(val_s_dl)
            eval_recon_loss += recon_loss.item() / len(val_s_dl)
            eval_cls_art_loss += cls_art_loss.item() / len(val_s_dl)
            eval_cls_art_acc += clf_art_acc / len(val_s_dl)
            eval_cls_dyn_loss += cls_dyn_loss.item() / len(val_s_dl)
            eval_cls_dyn_acc += clf_dyn_acc / len(val_s_dl)

            eval_cls_art_hat_loss += cls_art_hat_loss.item() / len(val_s_dl)
            eval_cls_art_hat_acc += clf_art_hat_acc / len(val_s_dl)
            eval_cls_dyn_hat_loss += cls_dyn_hat_loss.item() / len(val_s_dl)
            eval_cls_dyn_hat_acc += clf_dyn_hat_acc / len(val_s_dl)

            eval_kl_style_loss += kl_style_loss.item() / len(val_s_dl)
            eval_entropy_style += h_q_style.item() / len(val_s_dl)


        print('''Sup Eval: Recon: {:.4} | CLF Art Loss: {:.4} | Art Acc: {:.4} | CLF Dyn Loss: {:.4} | Dyn Acc: {:.4} | CLF Art Hat Loss: {:.4} | Art Acc Hat: {:.4} | CLF Dyn Hat Loss: {:.4} | Dyn Acc Hat: {:.4}'''.format(
                eval_recon_loss, eval_cls_art_loss, eval_cls_art_acc, eval_cls_dyn_loss, eval_cls_dyn_acc,
                eval_cls_art_hat_loss, eval_cls_art_hat_acc, eval_cls_dyn_hat_loss, eval_cls_dyn_hat_acc))
        
        eval_sup_writer.add_scalar('Recon', eval_recon_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('KL Style', eval_kl_style_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('Entropy', eval_entropy_style, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Art Loss', eval_cls_art_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Art Acc', eval_cls_art_acc, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Dyn Loss', eval_cls_dyn_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Dyn Acc', eval_cls_dyn_acc, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Art Hat Loss', eval_cls_art_hat_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Art Hat Acc', eval_cls_art_hat_acc, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Dyn Hat Loss', eval_cls_dyn_hat_loss, global_step=step_sup)
        eval_sup_writer.add_scalar('CLF Dyn Hat Acc', eval_cls_dyn_hat_acc, global_step=step_sup)

        # save model every epoch
        torch.save(model.state_dict(), save_path)
        normalizer.save_minmax()

        if step_unsup % 40000 == 0 and step_unsup > 0:
            for p in optimizer.param_groups:
                p['lr'] = args["lr"] / lr_factor
            lr_factor *= 2

        if ep % 10 == 0:
            # plot spectrograms
            audio, onset_pr, frame_pr, emotion_cls = train_s_ds[10]
            pr_visualize = onset_pr + frame_pr
            # pr = torch.cat([onset_pr, frame_pr], dim=-1)
            pr = onset_pr
            melspec = torch.transpose(wav_to_melspec(audio), 1, 2)[:, :-1, :]   # (b, 625, 128)
            melspec_original = wav_to_melspec(audio)
            
            # use log melspec
            pr = pr.cuda().unsqueeze(0)
            if args["melspec_mode"] == "log":
                melspec = torch.log(melspec + 1e-12).cuda()
            elif args["melspec_mode"] == "log-tanh":
                melspec = torch.nn.Tanh()(0.25 * torch.log(melspec + 1e-12)).cuda()
            elif args["melspec_mode"] == "log-minmax":
                melspec = normalizer.transform(torch.log(melspec + 1e-12)).cuda()
            
            melspec_hat, z_art_lst, art_cls_lst, mu_art_lst, var_art_lst, \
                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst, \
                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob, \
                    mu_art_lst_hat, var_art_lst_hat, mu_dyn_lst_hat, var_dyn_lst_hat, \
                    art_cls_lst_hat, dyn_cls_lst_hat = model(melspec, pr)
            
            if args["melspec_mode"] == "log":
                melspec_hat_denorm = torch.exp(melspec_hat).cuda().T.squeeze()
            elif args["melspec_mode"] == "log-tanh":
                def atanh(x):
                    return 0.5*torch.log((1+x)/(1-x) + 1e-12)
                melspec_hat_denorm = torch.exp(atanh(melspec_hat) * 4).cuda()
            elif args["melspec_mode"] == "log-minmax":
                melspec_hat_denorm = torch.exp(melspec_hat * (10 + 20) - 20).T.squeeze()

            # plot spectrograms
            fig = plt.figure(figsize=(8,8))
            melspec_db_1 = librosa.power_to_db(melspec_original.cpu().detach().numpy().squeeze(), ref=np.max)
            librosa.display.specshow(melspec_db_1, x_axis='time',
                                    y_axis='mel', sr=16000,
                                    fmax=8000)
            plt.colorbar(format='%+2.0f dB')
            train_unsup_writer.add_figure('spec_original', fig, global_step=step_unsup, close=True)

            fig = plt.figure(figsize=(8,8))
            melspec_db_2 = librosa.power_to_db(melspec_hat_denorm.cpu().detach().numpy().squeeze(), ref=np.max)
            librosa.display.specshow(melspec_db_2, x_axis='time',
                                    y_axis='mel', sr=16000,
                                    fmax=8000)
            plt.colorbar(format='%+2.0f dB')
            train_unsup_writer.add_figure('spec_recon', fig, global_step=step_unsup, close=True)

            # plot piano rolls
            fig = plt.figure(figsize=(8,8))
            plt.imshow(pr_visualize.squeeze().cpu().detach().numpy().T)
            train_unsup_writer.add_figure('onset_pr_original', fig, global_step=step_unsup, close=True)

            # plot latent space for style
            z_style_lst = []
            cls_style_lst = []

            for i, x_temp in tqdm(enumerate(train_s_dl), total=len(train_s_dl), desc='Running latents on train set:'):
                audio, onset_pr, frame_pr, emotion_cls = x_temp
                pr_visualize = onset_pr + frame_pr
                pr = onset_pr
                melspec = torch.transpose(wav_to_melspec(audio), 1, 2)[:, :-1, :]   # (b, 625, 128)
                melspec_original = wav_to_melspec(audio)
                
                # use log melspec
                pr = pr.cuda().unsqueeze(0)
                if args["melspec_mode"] == "log":
                    melspec = torch.log(melspec + 1e-12).cuda()
                elif args["melspec_mode"] == "log-tanh":
                    melspec = torch.nn.Tanh()(0.25 * torch.log(melspec + 1e-12)).cuda()
                elif args["melspec_mode"] == "log-minmax":
                    melspec = normalizer.transform(torch.log(melspec + 1e-12)).cuda()

                pr = pr.squeeze()
                melspec_hat, z_art_lst, art_cls_lst, mu_art_lst, var_art_lst, \
                    z_dyn_lst, dyn_cls_lst, mu_dyn_lst, var_dyn_lst, \
                    z_style, z_style_dist, cls_z_style_logits, cls_z_style_prob, \
                    mu_art_lst_hat, var_art_lst_hat, mu_dyn_lst_hat, var_dyn_lst_hat, \
                    art_cls_lst_hat, dyn_cls_lst_hat = model(melspec, pr)
                
                z_style_lst.append(z_style.cpu().detach())
                cls_style_lst.append(torch.argmax(cls_z_style_prob, dim=-1).squeeze().cpu().detach())
            
            z_style_lst = torch.cat(z_style_lst, dim=0).numpy()
            cls_style_lst = torch.cat(cls_style_lst, dim=0).cpu().detach().numpy()

            from sklearn.manifold import TSNE
            import seaborn as sns
            sns.set()

            print("Plotting TSNE...", end="\r")
            tsne = TSNE(n_components=2, verbose=0)  #metric='manhattan'
            tsne_features = tsne.fit_transform(z_style_lst)
            color = cls_style_lst
            palette = sns.color_palette("bright", len(set(color)))
            fig = plt.figure(figsize=(8,8))
            sns.scatterplot(tsne_features[:,0], tsne_features[:,1], palette=palette, hue=color, legend='full')
            train_unsup_writer.add_figure('tsne_z_style', fig, global_step=step_sup, close=True)
            print("Plotting TSNE...done.")


if __name__ == "__main__":

    # housekeeping
    with open('nms_latent_config.json') as f:
        args = json.load(f)
    if not os.path.isdir('params'):
        os.mkdir('params')
    if not os.path.isdir('logs'):
        os.mkdir('logs')
    save_path = 'params/{}_{}.pt'.format(args['name'], datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    save_path += '_{}'.format(args["melspec_mode"])
    save_path += '_{}'.format(args["percent"])

    NUM_EMOTIONS = 2
    MELSPEC_DIM = 80
    PR_DIM = 88

    # print("Loading performance style dict...", end="\r")
    # with open("data/performance_style_dict_v2.json", "r+") as f:
    #     performance_style_dict = json.load(f)
    # print("Loading performance style dict...done.")

    # s_percent, u_percent = str(args["percent"]), str(100 - args["percent"])
    # print("Supervised / Unsupervised percentage: {} / {}".format(s_percent, u_percent))

    # load unlabelled data
    # train_ds = MAESTRO(path='/data/MAESTRO', groups=['train_u'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # train_dl = DataLoader(train_ds, batch_size=args["batch_size"], shuffle=True, num_workers=0)
    # val_ds = MAESTRO(path='/data/MAESTRO', groups=['validation_u'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # val_dl = DataLoader(val_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)
    # test_ds = MAESTRO(path='/data/MAESTRO', groups=['test_u'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # test_dl = DataLoader(test_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)

    # load labelled data
    # train_s_ds = MAESTRO(path='/data/MAESTRO', groups=['train_s'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # train_s_dl = DataLoader(train_s_ds, batch_size=args["batch_size"], shuffle=True, num_workers=0)
    # val_s_ds = MAESTRO(path='/data/MAESTRO', groups=['validation_s'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # val_s_dl = DataLoader(val_s_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)
    # test_s_ds = MAESTRO(path='/data/MAESTRO', groups=['test_s'], sequence_length=320000,
    #                    performance_style_dict=performance_style_dict)
    # test_s_dl = DataLoader(test_s_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)

    # load all data
    train_s_ds = MAESTRO(path='/data/MAESTRO', groups=['train_all'], sequence_length=320000,
                performance_style_dict=None)
    train_s_dl = DataLoader(train_s_ds, batch_size=args["batch_size"], shuffle=True, num_workers=0)
    val_s_ds = MAESTRO(path='/data/MAESTRO', groups=['validation_all'], sequence_length=320000,
                performance_style_dict=None)
    val_s_dl = DataLoader(val_s_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)
    test_s_ds = MAESTRO(path='/data/MAESTRO', groups=['test_all'], sequence_length=320000,
                performance_style_dict=None)
    test_s_dl = DataLoader(test_s_ds, batch_size=args["batch_size"], shuffle=False, num_workers=0)

    # cls_lst_art = []
    # cls_lst_dyn = []
    # for i, x in enumerate(train_s_dl):
    #     audio, onset_pr, frame_pr, cls = x     # (b, 320000), (b, t=625, 88)
    #     cls_lst_art.append(cls[0])
    #     cls_lst_dyn.append(cls[1])
        
    # cls_lst_art = torch.cat(cls_lst_art, dim=0)
    # cls_lst_dyn = torch.cat(cls_lst_dyn, dim=0)
    # from collections import Counter
    # print("Supervised actual labels:", Counter(cls_lst_art.cpu().numpy()), Counter(cls_lst_dyn.cpu().numpy()))

    # load emotion data
    # train_emotion_ds = MAESTRO(path='/data/MAESTRO', groups=['train_emotion'], sequence_length=320000)
    # train_emotion_dl = DataLoader(train_emotion_ds, batch_size=args["batch_size"] // 4, shuffle=True, num_workers=0)
    # val_emotion_ds = MAESTRO(path='/data/MAESTRO', groups=['validation_emotion'], sequence_length=320000)
    # val_emotion_dl = DataLoader(val_emotion_ds, batch_size=args["batch_size"] // 4, shuffle=False, num_workers=0)
    # test_emotion_ds = MAESTRO(path='/data/MAESTRO', groups=['test_emotion'], sequence_length=320000)
    # test_emotion_dl = DataLoader(test_emotion_ds, batch_size=args["batch_size"] // 4, shuffle=False, num_workers=0)

    # load model
    model = NMSLatentDisentangledStyle(n_component=NUM_EMOTIONS, n_style=4)
    model.cuda()
    optimizer = optim.Adam(model.parameters(), lr=args['lr'], betas=(0.9, 0.98), eps=1e-9)

    wav_to_melspec = Spectrogram.MelSpectrogram(sr=16000, n_mels=MELSPEC_DIM)
    normalizer = Normalizer(mode="imagewise")

    # load writers
    current_time = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    train_log_dir = 'logs/'+args['name']+'_style_v1/'+current_time+'/train'
    eval_log_dir = 'logs/'+args['name']+'_style_v1/'+current_time+'/eval'
    train_unsup_writer = SummaryWriter(train_log_dir + "_unsup")
    train_sup_writer = SummaryWriter(train_log_dir + "_sup")
    eval_unsup_writer = SummaryWriter(eval_log_dir + "_unsup")
    eval_sup_writer = SummaryWriter(eval_log_dir + "_sup")
    
    training()