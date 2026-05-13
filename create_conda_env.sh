#! bin/bash

module load proxy/proxy_16
module load cuda12.9/toolkit/12.9
module load miniconda3/py312_2

source /archive/apps/miniconda/miniconda3/py312_2/etc/profile.d/conda.sh
conda create --name agrarian312 python=3.12 -y
conda activate agrarian312

pip install --upgrade pip
pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cu130
pip install --no-cache-dir -r dev_requirements.txt

conda deactivate
