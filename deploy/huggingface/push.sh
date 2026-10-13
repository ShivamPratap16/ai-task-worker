#!/usr/bin/env bash
# Publish the committed HEAD of this repo to a Hugging Face Docker Space.
#   deploy/huggingface/push.sh <hf-username>/<space-name>
# Git asks for your Hugging Face username and an access token with write permission as the password.
set -euo pipefail
SPACE="${1:?usage: deploy/huggingface/push.sh <hf-username>/<space-name>}"
REPO="$(git rev-parse --show-toplevel)"
REV="$(git -C "$REPO" rev-parse --short HEAD)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

git clone --depth 1 "https://huggingface.co/spaces/$SPACE" "$WORK/space"
git -C "$WORK/space" rm -rq --ignore-unmatch .
git -C "$REPO" archive HEAD | tar -x -C "$WORK/space"
cp "$REPO/deploy/huggingface/README.md" "$WORK/space/README.md"   # the Space's README carries its config

git -C "$WORK/space" add -A
git -C "$WORK/space" commit -qm "Deploy ai-task-worker $REV"
git -C "$WORK/space" push
echo "Pushed $REV. Build log: https://huggingface.co/spaces/$SPACE?logs=build"
