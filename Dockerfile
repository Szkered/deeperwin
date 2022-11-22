FROM nvidia/cuda:11.4.2-cudnn8-devel-ubuntu20.04

ARG DEBIAN_FRONTEND=noninteractive

RUN sed -i "s/archive.ubuntu.com/mirror.0x.sg/g" /etc/apt/sources.list

# Install dependencies
COPY apt_install.txt .
RUN apt-get update && apt-get install -y `cat apt_install.txt`

# Config pip
RUN ln -sf /usr/bin/python3 /usr/bin/python

# Upgrade pip, install py libs
RUN pip3 install --upgrade pip
RUN pip3 install jupyterlab autopep8 isort --upgrade

WORKDIR /app

RUN pip install -U wandb
RUN wandb login 20d729129686c9a3f766d60185d416b0acb7cef8

COPY . .
# RUN git clone https://github.com/Szkered/deeperwin.git
# WORKDIR /app/deeperwin
# RUN git checkout -b JG origin/JG
RUN pip install -e .

RUN pip uninstall jax jaxlib -y
RUN pip install --upgrade "jax[cuda]==0.3.15" -f https://storage.googleapis.com/jax-releases/jax_cuda_releases.html
RUN pip install protobuf==3.20.*
