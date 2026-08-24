# ASAC 데이터 엔지니어 2기 인프라 프로젝트 1조

## CI/CD

`main`에 변경 사항이 들어오면 다음 검증이 실행됩니다.

- Python 애플리케이션 테스트와 Docker 이미지 빌드
- `k8s/overlays/prod` Kustomize 렌더링
- Terraform provider 초기화, 포맷 검사, validate

애플리케이션 변경 시 CD workflow가 `app/`을 `eu-west-1` ECR에 SHA 태그로 빌드·push하고, `k8s/overlays/prod/kustomization.yaml`의 이미지 태그를 갱신합니다. Argo CD는 `main`의 `k8s/overlays/prod`를 감시하고 Argo Rollouts canary 절차를 수행합니다.

## GitHub 설정

Terraform 적용 후 출력되는 `github_actions_role_arn` 값을 저장소 변수로 등록합니다.

```text
Settings → Secrets and variables → Actions → Variables
AWS_ROLE_TO_ASSUME=<terraform output github_actions_role_arn>
```

Terraform plan을 GitHub Actions에서 실행하려면 다음 저장소 secret도 등록해야 합니다.

```text
TF_VAR_DB_PASSWORD=<16자 이상인 새 RDS 비밀번호>
```

`AWS_ROLE_TO_ASSUME`이 없으면 Terraform cloud plan은 의도적으로 건너뛰고 정적 검증만 수행합니다. CD는 AWS role이 없으면 명확한 오류로 중단됩니다.

## 로컬 검증

```bash
python3 -m pip install -r app/requirements-dev.txt
python3 -m pytest -q app/tests
docker build --pull --tag data-pipeline-app-ci ./app
kubectl kustomize k8s/overlays/prod >/tmp/rendered-production.yaml

cd terraform
terraform init -backend=false -input=false
terraform fmt -check -recursive
terraform validate
```

## 배포 전 조건

클러스터에는 `raffle-config` ConfigMap과 `raffle-secret` Secret이 미리 있어야 합니다. 애플리케이션은 다음 환경 변수를 사용합니다.

- ConfigMap: `DB_WRITER_HOST`, `DB_READER_HOST`, `DB_NAME`, `DB_USER`
- Secret: `DB_PASSWORD`, `SECRET_KEY`

기존 Terraform 코드에 평문으로 있던 RDS 비밀번호는 제거했습니다. 이전 값이 Git 이력에 남아 있으므로 실제 AWS 환경에서 즉시 비밀번호를 회전해야 합니다.
