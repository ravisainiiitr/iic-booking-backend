FROM docker.io/traefik:v3.7.0
RUN apk add --no-cache gettext \
  && mkdir -p /etc/traefik/acme \
  && touch /etc/traefik/acme/acme.json \
  && chmod 600 /etc/traefik/acme/acme.json
COPY ./compose/production/traefik/traefik.yml.template /etc/traefik/traefik.yml.template
COPY ./compose/production/traefik/entrypoint /entrypoint
RUN chmod +x /entrypoint
ENTRYPOINT ["/entrypoint"]
CMD ["traefik"]
