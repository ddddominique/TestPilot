.PHONY: dev stop

dev:
	@/bin/zsh scripts/dev.zsh

stop:
	docker compose stop postgres redis
