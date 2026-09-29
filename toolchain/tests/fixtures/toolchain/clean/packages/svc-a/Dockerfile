# lockgen stage base — pinned by DIGEST, tag documented below.
#   docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.14
FROM public.ecr.aws/docker/library/python:3.14@sha256:aaaabbbbccccddddeeeeffff0000111122223333444455556666777788889999 AS lockgen
RUN echo build

# runtime base — pinned by DIGEST, tag documented below.
#   docker buildx imagetools inspect public.ecr.aws/lambda/python:3.14
FROM public.ecr.aws/lambda/python:3.14@sha256:721e972e16ef178662bae4b0185a062c959d020045e015bb565e9d6167ff565a
COPY --from=lockgen /w /w

# stage-alias FROM — not a base image, must not be checked.
FROM lockgen AS final
