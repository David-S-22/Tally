FROM nginx:alpine
EXPOSE 3006

ENV NGINX_ENVSUBST_TEMPLATE_VARS='$PORT $BUDGETS_BACKEND_URL'

COPY public/ /app/public/
COPY nginx.conf /etc/nginx/templates/default.conf.template
