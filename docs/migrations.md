# Python 최소 backend migration

`python-backend/`의 Alembic migration만 `persona_minimal` PostgreSQL 스키마와
`persona_minimal.alembic_version`을 소유한다. 앱 시작 시 migration을 실행하지 않으며,
별도 migrator 작업이 배포 전에 실행한다.

```sh
cd python-backend
DATABASE_URL=postgresql://... uv run alembic upgrade head
```

runtime DB 계정에는 `persona_minimal` schema의 사용 권한, 앱 테이블
(`users`, `personas`, `idempotency_records`)의 `SELECT`, `INSERT`, `UPDATE`, 그리고
`alembic_version`의 `SELECT`만 준다. schema 생성·DDL·`alembic_version` 변경은 migration
계정만 수행한다. runtime 계정에는 `CREATE`, `ALTER`, `DROP` 및 schema 소유 권한을 주지 않는다.
