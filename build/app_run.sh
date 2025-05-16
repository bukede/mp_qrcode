#!/bin/bash
shopt -s extglob
set -o nounset
set -o errexit

str=$(basename $(pwd))
env="${str##*-}"

declare -A port_map=(["dev"]=7890 ["prod"]=7891)

mkdir -p logs &&
  nohup granian main:app --interface asgi --host 0.0.0.0 --port ${port_map[$env]} 2>&1 >>./logs/granian${env}.log &
