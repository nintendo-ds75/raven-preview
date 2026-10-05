.PHONY: up down logs status login seed shell db-shell test test-postgres ingest backup restore

up:
	./dev up

down:
	docker compose down

logs:
	docker compose logs -f app

status:
	docker compose ps

login:
	@docker compose exec -T app cat /data/dev-login-token

seed:
	docker compose exec -T app python -m bridge.bootstrap

shell:
	docker compose exec app sh

db-shell:
	docker compose exec db psql -U bridge -d bridge

test:
	docker compose exec -T app python -m unittest discover -s tests -q

test-postgres:
	docker compose exec -T -e BRIDGE_TEST_POSTGRES=1 app python -m unittest discover -s tests -p test_postgres.py -v

ingest:
	@test -n "$(REPO)" || (echo 'Usage: make ingest REPO=directory-name'; exit 1)
	docker compose exec -T app python -m bridge ingest "/repos/$(REPO)" --repo "$(REPO)"

backup:
	@mkdir -p backups
	docker compose exec -T db pg_dump -U bridge -d bridge -Fc > "backups/bridge-$$(date +%Y%m%d-%H%M%S).dump"

# Restore into a NEW database; never overwrite the running application's DB.
restore:
	@test -n "$(FILE)" -a -n "$(DB)" || (echo 'Usage: make restore FILE=backups/file.dump DB=bridge_restored'; exit 1)
	docker compose exec -T db createdb -U bridge "$(DB)"
	docker compose exec -T db pg_restore -U bridge -d "$(DB)" --exit-on-error < "$(FILE)"
