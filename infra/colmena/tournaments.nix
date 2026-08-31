{
  ...
}:
{
  services.nginx.virtualHosts."tournaments.example.com" = {
    forceSSL = true;
    useACMEHost = "example.com";

    locations."/" = {
      proxyPass = "http://192.0.2.10:4000";
      proxyWebsockets = true;
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
