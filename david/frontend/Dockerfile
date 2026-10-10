FROM nginx:alpine

ENV NGINX_ENVSUBST_TEMPLATE_VARS='$PORT $BACKEND_URL'

COPY . /app/
COPY ./styles /app/styles
COPY ./nginx.conf /etc/nginx/templates/default.conf.template

EXPOSE 3002
