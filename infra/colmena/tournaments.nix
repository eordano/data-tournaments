# tournaments.example — public TLS endpoint for the data-tournaments
# Phoenix LiveView dev server, reverse-proxied through the TLS host's
# wildcard cert to the dev machine over Tailscale.
#
# This file is a STAGING COPY for a colmena fleet repo. Apply with:
#
#   cp infra/colmena/tournaments.nix \
#      <fleet>/hosts/<tls-host>/services/tournaments.nix
#   # add `./tournaments.nix` to imports in services/default.nix
#   colmena apply --on <tls-host>
#
# OR for a smaller diff, just inline the virtualHost block into
# services/default.nix next to your existing vhost entries.
#
# On the Mac, run Phoenix with PHX_LISTEN_ALL=1 so it binds 0.0.0.0:4000
# instead of just 127.0.0.1:
#
#   nix develop --command bash -c '
#     set -a && . .env && set +a
#     cd ui && PORT=4000 PHX_LISTEN_ALL=1 mix phx.server
#   '
#
# Tailnet ACLs gate access (giga is on the tailnet, internet is not).
{
  ...
}:
{
  services.nginx.virtualHosts."tournaments.example" = {
    forceSSL = true;
    useACMEHost = "example";

    locations."/" = {
      proxyPass = "http://203.0.113.1:4000"; # studio over tailnet
      proxyWebsockets = true; # LiveView WS
      extraConfig = ''
        # LiveView pushes can be slow during heavy DSPy/GEPA optimization
        # runs — bump timeouts so a long-running click doesn't 504.
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
        send_timeout 600s;

        # Forward real client info; Phoenix dev's check_origin is false
        # but be explicit for any future prod-like deploy.
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Real-IP $remote_addr;
      '';
    };
  };
}
