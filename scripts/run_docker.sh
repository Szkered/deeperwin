COMMIT_HASH=$(git log -1 --format=%h)
docker run --name dev --cap-add=SYS_PTRACE --security-opt seccomp=unconfined -t -d $1:${COMMIT_HASH} bash
