COMMIT_HASH=$(git log -1 --format=%h)
docker build -t $1:${COMMIT_HASH} -f Dockerfile .
echo successfully build docker tag: $1:${COMMIT_HASH}
