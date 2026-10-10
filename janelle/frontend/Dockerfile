FROM nginx:alpine

EXPOSE 3001

ENV NGINX_ENVSUBST_TEMPLATE_VARS='$PORT $TRANSACTIONS_BACKEND_URL'

COPY public/ /app/public/
COPY nginx.conf /etc/nginx/templates/default.conf.template
