FROM nginx:alpine
EXPOSE 3000

COPY public/ /app/public/
COPY nginx.conf /etc/nginx/templates/default.conf.template
# http-context directives (rate-limit zone) for the server block in nginx.conf.
COPY 00-limits.conf /etc/nginx/templates/00-limits.conf.template
