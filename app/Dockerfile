FROM python:3.12-alpine
WORKDIR /app
COPY app /app/app
COPY web /app/web
COPY openapi.json /app/openapi.json
RUN mkdir /data && chown 1000:1000 /data
USER 1000:1000
ENV DATA_DIR=/data PORT=8809
EXPOSE 8809
CMD ["python", "/app/app/server.py"]
