FROM nginx:1.27-alpine
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY index.html /usr/share/nginx/html/index.html
# Only substitute our own variables, never nginx's ($uri, $1, ...)
ENV NGINX_ENVSUBST_FILTER=^(GITEA|IMPORTER)_
COPY favicon.svg manifest.webmanifest /usr/share/nginx/html/
COPY icons/*.png /usr/share/nginx/html/icons/
