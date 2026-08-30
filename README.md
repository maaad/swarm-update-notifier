# swarm-update-notifier

Small Docker Swarm service-update notifier for Telegram.

It listens to Docker Engine `service update` events on a Swarm manager, waits for the Swarm update to reach a terminal state, compares the previous and current service image, and sends a single Telegram message.

## Behavior

- Initial startup creates a baseline and sends no notifications.
- By default the provided stack only watches services with the service label `swarm.notify=true`.
- By default only image changes trigger notifications (`NOTIFY_IMAGE_ONLY=true`).
- State is persisted in `/data/state.json` so a notifier restart can detect an update it missed while restarting.
- Success: `UpdateStatus.State=completed`.
- Failure/rollback states are also reported.

## Add notification label to a service

The label must be a **Swarm service label**, therefore put it under `deploy.labels`:

```yaml
services:
  app:
    image: example/app:latest@sha256:...
    deploy:
      labels:
        swarm.notify: "true"
```

## Telegram secret

Create a Swarm secret named `telegram_bot_token` containing only the bot token.

CLI example:

```sh
printf '%s' '123456:ABC...' | docker secret create telegram_bot_token -
```

Set `TELEGRAM_CHAT_ID` as a Portainer stack environment variable.

## Deployment

Build/publish the image through the included GitHub Actions workflow, then deploy `stacks/notifier/compose.yml` from Portainer GitOps.

For a private GHCR package, configure GHCR credentials in Portainer so the Swarm manager can resolve/pull the image. Alternatively make the package public.

## Security

Access to `/var/run/docker.sock` is effectively root-equivalent access to the Docker host/Swarm. The notifier only performs GET requests, but the socket itself cannot enforce read-only API access. For stronger isolation, place a Docker socket proxy in front of it and allow only GET access to events/services/tasks/nodes.
