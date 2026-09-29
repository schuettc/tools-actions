# deps base — a stage the app stage resolves to by alias below.
#   docker buildx imagetools inspect python:3.12
FROM python:3.12-slim-bookworm@sha256:1111222233334444555566667777888899990000aaaabbbbccccddddeeeeffff AS deps
RUN echo deps

# stage-alias FROM — must resolve to `deps`, not be checked as a base image.
FROM deps AS app
RUN echo app
