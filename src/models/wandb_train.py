import os
import time
import argparse
import wandb
import numpy as np
import torch
from torchvision import transforms
from torch.utils.data import DataLoader
from torchvision.utils import make_grid

from resnet_vae import ResNet_VAE
import sys
# caution: path[0] is reserved for script path (or '' in REPL)
sys.path.insert(1, '../data')
from shapes_dataset import ShapesDataset
from lines_dataset import LinesDataset
from utils import ThresholdTransform, check_mkdir
from training_utils import train, validation, MaterialSimilarityLoss, ExponentialScheduler, LossCoefficientScheduler, learning_rate_switcher, get_learning_rate, change_learning_rate, seed_everything, generate_from_noise, generate_reconstructions, write_gradient_stats


def run_training(epochs, a_mse, a_content, a_style, a_spst, beta, 
                content_layer, style_layer, 
                learning_rate=1e-3, fine_tune_lr=0.0005, 
                spatial_stat_loss_reduction='mean', normalize_spatial_stat_tensors=False, soft_equality_eps=0.25,
                batch_size=32, CNN_embed_dim=256, dropout_p=0.2, 
                log_interval=2, save_interval=20, resume_training=False, last_epoch=0, 
                schedule_KLD=False, schedule_spst=False, 
                dataset_name='shapes',
                debugging=False):
    
    seed=110
    seed_everything(seed)
    
    save_dir = os.path.join(os.getcwd(), "models")
    run_name = "resnetVAE_shapesData_" + f"lr{learning_rate}" + f"bs{batch_size}" +\
          f"_a_mse_{a_mse}" + f"_a_content_{a_content}" + f"_a_style_{a_style}" +\
              f"_a_spst_{a_spst}" + f"_content_layer_{content_layer}" +\
                 f"_style_layer_{style_layer}" + "_sum_reduction_mse_loss" +\
                      f"_KLD_scheduling_{schedule_KLD}" + f"spatial_stats_loss_scheduled_{schedule_spst}"
    
    save_model_path = os.path.join(save_dir, run_name)
    check_mkdir(save_model_path)    

    # alternatively, you could save in W&B but depending on the network speed, uploading the models can be slow.
    #save_model_path = wandb.run.dir

    # Detect devices
    use_cuda = torch.cuda.is_available()   
    device = torch.device("cuda" if use_cuda else "cpu")
    if use_cuda:
        print("Using", torch.cuda.device_count(), "GPU!")
    else:
        print("Training on CPU!")

    # Load Data
    res_size = 224
    # Define the transformation to apply to the data
    transform = transforms.Compose([
        transforms.ToTensor(),  # Convert the image to tensor
        transforms.Normalize((0.5,), (0.5,)),  # Normalize the pixel values to the range [-1, 1]
        #transforms.Lambda(lambda x: x.repeat(3, 1, 1)),
        transforms.Resize([res_size, res_size], antialias=True),
        ThresholdTransform(thr_255=240),
    ])

    # Initialize your Dataset
    #dataset = CustomDataset('labels.csv', 'images', transformations)
    if dataset_name=='lines':
        data_dir = 'lines'
        dataset = LinesDataset(os.path.join(os.getcwd(), f'data/{data_dir}/labels.csv'), os.path.join(os.getcwd(), f'data/{data_dir}/images'), transform)
    elif dataset_name=='shapes':
        data_dir = 'shapes'
        dataset = ShapesDataset(os.path.join(os.getcwd(), f'data/{data_dir}/labels.csv'), os.path.join(os.getcwd(), f'data/{data_dir}/images'), transform)
    train_dataset, valid_dataset = torch.utils.data.random_split(dataset, [int(len(dataset)*0.7), int(len(dataset)) - int(len(dataset)*0.7)])
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=4)
    valid_loader = DataLoader(valid_dataset, batch_size=batch_size, shuffle=False)

    # EncoderCNN architecture
    CNN_fc_hidden1, CNN_fc_hidden2 = 1024, 1024
    # Build model
    resnet_vae = ResNet_VAE(fc_hidden1=CNN_fc_hidden1, fc_hidden2=CNN_fc_hidden2, drop_p=dropout_p, CNN_embed_dim=CNN_embed_dim, device=device).to(device)
    resnet_vae.resnet.requires_grad_(False)
    wandb.watch(resnet_vae)
    model_params = list(resnet_vae.parameters())
    optimizer = torch.optim.Adam(model_params, lr=learning_rate)
    beta_scheduler = ExponentialScheduler(start=0.005, max_val=beta, epochs=epochs) # start = 256/(224*224) = (latent space dim)/(input dim)
    loss_function = MaterialSimilarityLoss(device, content_layer=content_layer, style_layer=style_layer, 
                                           spatial_stat_loss_reduction=spatial_stat_loss_reduction,
                                           normalize_spatial_stat_tensors=normalize_spatial_stat_tensors, 
                                           soft_equality_eps=soft_equality_eps)
    a_spst_scheduler = LossCoefficientScheduler(a_spst, epochs, mode="sigmoid")

    print({
        "seed": seed,
        "run_name": run_name, 
        #"content_layer_coeffs": loss_function.content_layer_coefficients,
        #"style_layer_coeffs": loss_function.style_layer_coefficients,
        })
    
    if resume_training:
        assert last_epoch != None
        resnet_vae.load_state_dict(torch.load(os.path.join(save_model_path,f'model_epoch{last_epoch}.pth')))
        optimizer.load_state_dict(torch.load(os.path.join(save_model_path,f'optimizer_epoch{last_epoch}.pth')))
        print("Resuming pretrained model...")
    else:
        last_epoch = 0


    #start training
    print("Started training.")
    for epoch in range(last_epoch, epochs):
        # schedule the learning rate
        if epoch > int(epochs*0.9):
            optimizer = change_learning_rate(optimizer, fine_tune_lr)
        
        # schedule beta
        if schedule_KLD:
            beta = beta_scheduler.get_beta(epoch)
        else:
            beta=1

        # train, test model
        start = time.time()
        X_train, y_train, z_train, mu_train, logvar_train, training_losses, training_input_autocorr, training_recon_autocorr, mse_grads, spst_grads, kld_grads = train(log_interval, resnet_vae, loss_function, device, train_loader, optimizer, epoch, save_model_path, a_mse, a_content, a_style, a_spst, beta, debugging)
        X_test, y_test, z_test, mu_test, logvar_test, validation_losses, validation_input_autocorr, validation_recon_autocorr = validation(resnet_vae, loss_function, device, valid_loader, a_mse, a_content, a_style, a_spst, beta, debugging)
        mse_training_loss, content_training_loss, style_training_loss, spst_training_loss, kld_training_loss, overall_training_loss = training_losses
        mse_loss, content_loss, style_loss, spst_loss, kld_loss, overall_loss = validation_losses
        metrics = {
            "mse_training_loss": mse_training_loss, 
            "mse_validation_loss": mse_loss, 
            "spatial_stats_training_loss": spst_training_loss,
            "spatial_stats_validation_loss": spst_loss,
            "KLD_training_loss": kld_training_loss,
            "KLD_validation_loss": kld_loss,
            "overall_training_loss": overall_training_loss,
            "overall_validation_loss": overall_loss,
            "content_validation_loss": content_loss, 
            "content_training_loss": content_training_loss, 
            "style_training_loss": style_training_loss,
            "style_validation_loss": style_loss,
            "mu_training": mu_train,
            "mu_test": mu_test,
            "logvar_train": logvar_train,
            "logvar_test": logvar_test,
            "alpha_mse": a_mse,
            "alpha_spst": a_spst,
            }
        wandb.log(metrics)

        # schedule the spst loss value
        if schedule_spst:
            a_spst = a_spst_scheduler.step()
            a_mse = 1 - a_spst
        
        save_condition = True if debugging else (epoch + 1) % save_interval == 0
        if save_condition:
            torch.save(resnet_vae.state_dict(), os.path.join(save_model_path, 'model_epoch{}.pth'.format(epoch + 1)))  # save motion_encoder
            torch.save(optimizer.state_dict(), os.path.join(save_model_path, 'optimizer_epoch{}.pth'.format(epoch + 1)))      # save optimizer
            np.save(os.path.join(save_model_path, 'X_train_epoch{}.npy'.format(epoch + 1)), X_train) #save last batch
            np.save(os.path.join(save_model_path, 'y_train_epoch{}.npy'.format(epoch + 1)), y_train)
            np.save(os.path.join(save_model_path, 'z_train_epoch{}.npy'.format(epoch + 1)), z_train)
            print("Data and model-optimizer params saved successfully.")
            
            grid = generate_from_noise(resnet_vae, device, 16)
            imgs = wandb.Image(grid, caption="(Genearted image for validation, Genearted image autocorrelation)")
            wandb.log({'Validation generated images from noise': imgs})
            print("Validation images generated from noise successfully.")

            # training_input_autocorr = training_input_autocorr.unsqueeze(1)
            # training_recon_autocorr = training_recon_autocorr.unsqueeze(1)
            training_loss_autocorr_grid = torch.cat([training_input_autocorr, training_recon_autocorr], axis=2)
            training_loss_autocorr_grid = make_grid(training_loss_autocorr_grid, nrow=8, padding=1)
            imgs = wandb.Image(training_loss_autocorr_grid, caption="From inside the loss function. top: training input image autocorrelation, bottom: training input reconstructed image autocorrelation")
            wandb.log({'Training autocorr images from inside the spst loss function': imgs})
            print("Training autocorr images from inside the spst loss function saved successfully.")

            # validation_input_autocorr = validation_input_autocorr.unsqueeze(1)
            # validation_recon_autocorr = validation_recon_autocorr.unsqueeze(1)
            validation_loss_autocorr_grid = torch.cat([validation_input_autocorr, validation_recon_autocorr], axis=2)
            validation_loss_autocorr_grid = make_grid(validation_loss_autocorr_grid, nrow=8, padding=1)
            imgs = wandb.Image(validation_loss_autocorr_grid, caption="From inside the loss function. top: validation input image autocorrelation, bottom: validation input reconstructed image autocorrelation")
            wandb.log({'Validation autocorr images from inside the spst loss function': imgs})
            print("Validation autocorr images from inside the spst loss function saved successfully.")

        # save gradient stats
        total_grads = write_gradient_stats(resnet_vae)
        wandb.log({'Total gradients mean': total_grads.mean(), "Total gradients std": total_grads.std()})
        wandb.log({'mse gradients mean': np.mean(mse_grads), "mse gradients std": np.std(mse_grads)})
        wandb.log({'spst gradients mean': np.mean(spst_grads), "spst gradients std": np.std(spst_grads)})
        wandb.log({'kl gradients mean': np.mean(kld_grads), "kl gradients std": np.std(kld_grads)})
        print("Gradients saved successfully.")

        print(f"epoch time elapsed {time.time() - start} seconds")
        print("-------------------------------------------------")


    print(f"Finished training for {run_name}.")



