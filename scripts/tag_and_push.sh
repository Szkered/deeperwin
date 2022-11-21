COMMIT_HASH=$(git log -1 --format=%h)
docker tag $1:${COMMIT_HASH} registry.git.insea.io/sail/common/image_registry/sail/$1:${COMMIT_HASH} \
    && docker push registry.git.insea.io/sail/common/image_registry/sail/$1:${COMMIT_HASH}

docker tag $1:${COMMIT_HASH} registry.git.insea.io/sail/common/image_registry/sail/$1:latest \
    && docker push registry.git.insea.io/sail/common/image_registry/sail/$1:latest
