#!/bin/sh
set -eu

DOMAIN="${DOMAIN:-_}"
export DOMAIN

envsubst '${DOMAIN}' < /etc/nginx/templates/default.conf.template > /etc/nginx/conf.d/default.conf

exec nginx -g 'daemon off;'
