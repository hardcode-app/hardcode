# Not built: the BuildKit image the workflows' image builders run
# (.github/actions/buildx gives it to docker/setup-buildx-action, which would
# otherwise pull the moving buildx-stable-1 tag). A Dockerfile, so that
# Dependabot moves it a week after a release and CI's Pin ages dates it
# (maintenance/pins.py), like every other image.
FROM moby/buildkit:v0.34.1@sha256:99368f3e46177c6405b9740e4461d91cee004128daeecca78d4ceac50ec1da5c
