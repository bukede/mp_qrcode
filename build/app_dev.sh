#!/bin/bash
shopt -s extglob
set -o nounset
set -o errexit

export WATCHFILES_FORCE_POLLING=True

mkdir -p logs &&
  uvicorn main:app --reload --reload-dir ./ --reload-delay 1 --host 0.0.0.0 --port 7890
