# adapted from https://github.com/pytorch/examples/blob/master/mnist/main.py
from __future__ import print_function

import argparse
import time

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler
from torchvision import datasets, transforms

from fairscale.nn.data_parallel import ShardedDataParallel
from fairscale.optim import OSS

WORLD_SIZE = 2
OPTIM = torch.optim.RMSprop
BACKEND = dist.Backend.NCCL if torch.cuda.is_available() else dist.Backend.GLOO


def dist_init(rank, world_size, backend):
    print(f"Using backend: {backend}")
    dist.init_process_group(backend=backend, init_method="tcp://localhost:29501", rank=rank, world_size=world_size)


class Net(nn.Module):
    def __init__(self):
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = nn.Conv2d(32, 64, 3, 1)
        self.dropout1 = nn.Dropout2d(0.25)
        self.dropout2 = nn.Dropout2d(0.5)
        self.fc1 = nn.Linear(9216, 128)
        self.fc2 = nn.Linear(128, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = F.relu(x)
        x = self.conv2(x)
        x = F.relu(x)
        x = F.max_pool2d(x, 2)
        x = self.dropout1(x)
        x = torch.flatten(x, 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        output = F.log_softmax(x, dim=1)
        return output


def train(rank, args, use_cuda):
    # SETUP
    dist_init(rank, WORLD_SIZE, BACKEND)
    if use_cuda:
        torch.cuda.set_device(rank)

    device = torch.device(rank) if use_cuda else torch.device("cpu")

    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    dataset = datasets.MNIST("../data", train=True, download=True, transform=transform)
    sampler = DistributedSampler(dataset, num_replicas=WORLD_SIZE, rank=rank)
    kwargs = {"batch_size": args.batch_size, "sampler": sampler}
    if use_cuda:
        kwargs.update({"num_workers": 1, "pin_memory": True, "shuffle": True},)

    train_loader = DataLoader(dataset=dataset, **kwargs)
    model = Net().to(device)
    loss_fn = nn.CrossEntropyLoss()

    optimizer = OSS(params=model.parameters(), optim=torch.optim.Adadelta, lr=1e-4)
    model = ShardedDataParallel(model, optimizer,)

    # Reset the memory use counter
    if use_cuda:
        torch.cuda.reset_peak_memory_stats(rank)
        torch.cuda.synchronize(rank)

    training_start = time.monotonic()
    model.train()

    measurements = []
    for epoch in range(args.epochs):
        sampler.set_epoch(epoch)
        epoch_start = time.monotonic()
        for batch_idx, (data, target) in enumerate(train_loader):
            data, target = data.to(device), target.to(device)

            def closure():
                model.zero_grad()
                outputs = model(data)
                loss = loss_fn(outputs, target)
                loss.backward()
                return loss

            optimizer.step(closure)

        epoch_end = time.monotonic()

    if use_cuda:
        torch.cuda.synchronize(rank)
    training_stop = time.monotonic()
    print("Total Time:", training_stop - training_start)


def main():
    # Training settings
    parser = argparse.ArgumentParser(description="PyTorch MNIST Example")
    parser.add_argument(
        "--batch_size", type=int, default=64, metavar="N", help="input batch size for training (default: 64)"
    )
    parser.add_argument(
        "--test_batch_size", type=int, default=1000, metavar="N", help="input batch size for testing (default: 1000)"
    )
    parser.add_argument("--epochs", type=int, default=14, metavar="N", help="number of epochs to train (default: 14)")
    parser.add_argument("--lr", type=float, default=1.0, metavar="LR", help="learning rate (default: 1.0)")
    parser.add_argument("--gamma", type=float, default=0.7, metavar="M", help="Learning rate step gamma (default: 0.7)")
    parser.add_argument("--no_cuda", action="store_true", default=False, help="disables CUDA training")
    parser.add_argument("--dry_run", action="store_true", default=False, help="quickly check a single pass")
    parser.add_argument("--seed", type=int, default=1, metavar="S", help="random seed (default: 1)")
    parser.add_argument(
        "--log_interval",
        type=int,
        default=10,
        metavar="N",
        help="how many batches to wait before logging training status",
    )
    parser.add_argument("--save_model", action="store_true", default=False, help="For Saving the current Model")
    args = parser.parse_args()
    use_cuda = not args.no_cuda and torch.cuda.is_available()

    mp.spawn(
        train, args=(args, use_cuda), nprocs=WORLD_SIZE, join=True,
    )


if __name__ == "__main__":
    main()
