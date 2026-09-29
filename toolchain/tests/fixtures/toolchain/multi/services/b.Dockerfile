ARG PY=3.14
FROM public.ecr.aws/lambda/python:${PY}
RUN echo build
