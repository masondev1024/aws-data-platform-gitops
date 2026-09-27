from pathlib import Path
import re
import shutil
import subprocess
import stat
import os
import json

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_LAB = REPO_ROOT / "platform" / "live-lab"


def read(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


def test_terraform_is_isolated_backendless_and_provider_pinned():
    versions = read("platform/live-lab/terraform/versions.tf")
    main = read("platform/live-lab/terraform/main.tf")

    assert 'version = "= 5.100.0"' in versions
    assert 'version = "= 4.3.0"' in versions
    assert "backend " not in versions + main
    assert "allowed_account_ids = [var.aws_account_id]" in main
    assert 'Project       = "kyobo-platform-live-lab"' in main
    assert 'Scope         = "ephemeral-lane-e"' in main


def test_terraform_requires_explicit_approval_identity_cost_and_recovery_inputs():
    variables = read("platform/live-lab/terraform/variables.tf")
    required_without_defaults = [
        "aws_region",
        "aws_account_id",
        "operator_cidr",
        "operator_name",
        "approval_id",
        "session_id",
        "recovery_contact",
        "apply_approval_phrase",
        "availability_zones",
    ]

    for name in required_without_defaults:
        block = re.search(rf'variable "{name}" \{{(?P<body>.*?)\n\}}', variables, re.DOTALL)
        assert block is not None, name
        assert "default" not in block.group("body"), name

    main = read("platform/live-lab/terraform/main.tf")
    assert 'var.apply_approval_phrase == "APPROVED_FOR_EPHEMERAL_APPLY"' in main
    assert "data.aws_caller_identity.current.account_id == var.aws_account_id" in main
    assert 'support_type = "STANDARD"' in main
    assert 'default     = "1.36"' in variables
    assert "default     = 5.50" in variables
    assert "default     = 3" in variables
    assert "var.cost_budget_usd <= 5.50" in variables
    assert "var.max_session_hours <= 3" in variables
    assert "endpoint_private_access = true" in main
    assert "endpoint_public_access  = true" in main
    assert 'can(regex("/32$", var.operator_cidr))' in variables
    assert 'var.operator_cidr != "0.0.0.0/0"' in variables


def test_root_aws_resources_depend_on_approval_gate_and_alb_controller_bootstrap_exists():
    main = read("platform/live-lab/terraform/main.tf")
    outputs = read("platform/live-lab/terraform/outputs.tf")
    policy = read("platform/live-lab/terraform/policies/aws-load-balancer-controller-policy.json")

    for resource_name in [
        "aws_vpc\" \"lab",
        "aws_iam_role\" \"eks_cluster",
        "aws_iam_role\" \"eks_node",
        "aws_ecr_repository\" \"app",
        "aws_iam_policy\" \"aws_load_balancer_controller",
        "aws_iam_role\" \"aws_load_balancer_controller",
        "aws_wafv2_web_acl\" \"lab",
    ]:
        pattern = rf'resource "{resource_name}" \{{(?P<body>.*?)\n\}}'
        match = re.search(pattern, main, re.DOTALL)
        assert match is not None, resource_name
        assert "depends_on = [terraform_data.approval_gate]" in match.group("body")

    assert "aws_iam_openid_connect_provider" in main
    assert "system:serviceaccount:kube-system:aws-load-balancer-controller" in main
    assert "aws_load_balancer_controller_role_arn" in outputs
    assert "elasticloadbalancing:CreateLoadBalancer" in policy
    assert "elasticloadbalancing:CreateTargetGroup" in policy
    assert "elasticloadbalancing:DescribeListenerCertificates" in policy


def test_alb_controller_create_time_tag_permission_is_scoped_and_conditioned():
    policy = json.loads(read("platform/live-lab/terraform/policies/aws-load-balancer-controller-policy.json"))
    create_time_add_tags = [
        statement
        for statement in policy["Statement"]
        if statement.get("Effect") == "Allow"
        and "elasticloadbalancing:AddTags" in statement.get("Action", [])
        and statement.get("Condition", {}).get("StringEquals", {}).get("elasticloadbalancing:CreateAction")
    ]

    assert len(create_time_add_tags) == 1
    statement = create_time_add_tags[0]
    assert statement["Resource"] == "arn:aws:elasticloadbalancing:${region}:${account}:*"
    assert statement["Condition"]["StringEquals"]["elasticloadbalancing:CreateAction"] == [
        "CreateLoadBalancer",
        "CreateTargetGroup",
        "CreateListener",
        "CreateRule",
    ]
    assert statement["Condition"]["StringEquals"]["aws:RequestTag/elbv2.k8s.aws/cluster"] == "${cluster_name}"
    assert all(item.get("Resource") != "*" for item in create_time_add_tags)


def test_live_deploy_accepts_the_observed_regional_elb_hostname_shape():
    deploy = read("platform/live-lab/scripts/deploy_live_lab.sh")
    bootstrap = read("platform/live-lab/manifests/bootstrap/resources/raffle-config.yaml")

    assert '[[ "$alb_dns" == *.ap-northeast-2.elb.amazonaws.com ]]' in deploy
    assert '*.elb.ap-northeast-2.amazonaws.com' not in deploy
    assert 'TRUSTED_HOSTS: "*.ap-northeast-2.elb.amazonaws.com"' in bootstrap


def test_argo_weighted_ingress_backend_uses_aws_action_annotation_port():
    ingress = yaml.safe_load(read("k8s/base/ingress.yaml"))
    application_path = next(
        path
        for path in ingress["spec"]["rules"][0]["http"]["paths"]
        if path["path"] == "/"
    )

    assert application_path["backend"]["service"]["name"] == "data-pipeline-svc-stable"
    assert application_path["backend"]["service"]["port"] == {"name": "use-annotation"}


def test_synchronized_refresh_profile_is_one_bounded_request_per_virtual_user():
    loadtest = read("loadtest/raffle.js")
    wrapper = read("scripts/run_live_lab_k6.sh")

    assert "mode === 'synchronized-refresh'" in loadtest
    assert "executor: 'per-vu-iterations'" in loadtest
    assert "const refreshVus = Number(__ENV.REFRESH_VUS || 10000)" in loadtest
    assert 'elif [[ "$MODE" == "synchronized-refresh" ]]; then\n  planned=10000' in wrapper
    assert '"REFRESH_VUS=10000"' in wrapper


def test_live_lab_analysis_template_targets_the_installed_prometheus_service():
    kustomization = yaml.safe_load(read("platform/live-lab/manifests/app/kustomization.yaml"))
    patch_path = "patches/patch-analysis-template-prometheus-url.yaml"
    assert any(patch.get("path") == patch_path for patch in kustomization.get("patches", []))

    operations = yaml.safe_load(read(f"platform/live-lab/manifests/app/{patch_path}"))
    expected = "http://live-lab-observability-prometheus.monitoring.svc.cluster.local:9090"
    assert len(operations) == 5
    assert all(item.get("op") == "replace" and item.get("value") == expected for item in operations)
    assert [item.get("path") for item in operations] == [
        f"/spec/metrics/{index}/provider/prometheus/address" for index in range(5)
    ]


def test_canary_load_profile_requires_every_business_apply_to_succeed():
    loadtest = read("loadtest/raffle.js")
    assert "raffle_canary_apply_attempts: ['count>=3900']" in loadtest
    assert "raffle_canary_apply_successes: ['count>=3900']" in loadtest


def test_canary_error_rate_gate_does_not_tolerate_a_threshold_breach():
    analysis = yaml.safe_load(read("k8s/base/analysis-template.yaml"))
    metric = next(
        item for item in analysis["spec"]["metrics"] if item["name"] == "canary-error-rate"
    )

    assert metric["failureLimit"] == 0


def test_bounded_apply_vus_override_is_validated_and_reserved_consistently():
    wrapper = read("scripts/run_live_lab_k6.sh")

    assert 'apply_vus="${APPLY_VUS:-10}"' in wrapper
    assert '[[ "$apply_vus" =~ ^[0-9]+$ ]]' in wrapper
    assert '(( apply_vus >= 1 && apply_vus <= 200 ))' in wrapper
    assert 'plan --mode "$MODE" --apply-vus "$apply_vus"' in wrapper
    assert 'planned=$((apply_vus * 4))' in wrapper
    assert '"APPLY_VUS=$apply_vus"' in wrapper


def test_successful_read_replica_cutover_restores_cronjob_after_writer_verification():
    drill = read("scripts/rds-failover-drill.sh")
    section = drill.split("promote_replica_and_cutover() {", 1)[1].split("\n}\n\nmain()", 1)[0]

    restore = 'kube patch cronjob "$CRONJOB_NAME" --type merge -p "{\\"spec\\":{\\"suspend\\":${ORIGINAL_CRON_SUSPEND:-false}}}"'
    assert restore in section
    final_marker_verification = section.rindex(
        'helper_job verify-marker "$image" --role writer --run-id "$RUN_ID" --username "$SYNTHETIC_USERNAME"'
    )
    assert section.index(restore) > final_marker_verification
    assert section.index('event "promote-replica" "completed"') > section.index(restore)


def test_drill_failure_recovery_runs_for_explicit_exit_paths():
    drill = read("scripts/rds-failover-drill.sh")

    assert "trap on_error EXIT" in drill
    assert "trap on_error ERR" not in drill


def test_failed_helper_jobs_are_detected_without_waiting_for_timeout():
    drill = read("scripts/rds-failover-drill.sh")

    assert '.get("type")=="Failed"' in drill
    assert '.get("type")=="Complete"' in drill
    assert "wait-marker \"$app_image\" --role reader" in drill


def test_reader_replication_health_is_verified_before_writers_are_quiesced():
    drill = read("scripts/rds-failover-drill.sh")
    main = drill.split("main() {", 1)[1]

    assert "wait_for_reader_replication_health" in main
    assert "StatusInfos[?StatusType=='read replication'].Normal" in drill
    assert '[[ "$replication_normal" == "True" ]]' in drill
    assert main.index("wait_for_reader_replication_health") < main.index("quiesce_writers")
    assert main.count("wait_for_reader_replication_health") == 2
    assert main.index("wait-marker \"$app_image\"") < main.rindex("wait_for_reader_replication_health")
    assert main.rindex("wait_for_reader_replication_health") < main.index("require_two_fresh_lag_readings")


def test_replica_lag_gate_uses_fail_closed_validator_for_negative_sentinels():
    drill = read("scripts/rds-failover-drill.sh")

    assert "scripts/validate_rds_replica_lag.py" in drill


def test_trivy_exceptions_are_scoped_to_short_lived_lab_risks():
    main = read("platform/live-lab/terraform/main.tf")

    assert "#trivy:ignore:AVD-AWS-0039:exp:2026-10-31" in main
    assert "#trivy:ignore:AVD-AWS-0040:exp:2026-10-31" in main
    assert "#trivy:ignore:AVD-AWS-0164:exp:2026-10-31" in main
    assert "customer-managed KMS adds region-dependent cost and remains gated" in main
    assert "operator_cidr /32" in main
    assert "No SSH path is created" in main


def test_terraform_builds_private_multiaz_rds_and_async_replica_without_nat():
    terraform_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (LIVE_LAB / "terraform").glob("*.tf")
    )

    forbidden_tokens = [
        "aws_nat_gateway",
        "aws_wafv2_web_acl_association",
        "force_destroy",
        "deletion_protection.enabled=true",
    ]
    for token in forbidden_tokens:
        assert token not in terraform_text

    assert "force_delete = true" in terraform_text
    assert "unique session ID" in read("platform/live-lab/terraform/main.tf")
    assert 'engine            = "mysql"' in terraform_text
    assert 'instance_class    = "db.t3.small"' in terraform_text
    assert 'multi_az               = true' in terraform_text
    assert 'storage_encrypted = true' in terraform_text
    assert "replicate_source_db = aws_db_instance.mysql_primary.arn" in terraform_text
    assert "publicly_accessible    = false" in terraform_text
    assert "password_wo         = ephemeral.aws_secretsmanager_secret_version.db_master_password.secret_string" in terraform_text
    assert "password_wo_version = var.db_password_wo_version" in terraform_text
    assert 'required_version = ">= 1.11.0"' in read("platform/live-lab/terraform/versions.tf")
    assert "egress      = []" in terraform_text
    assert "deletion_protection.enabled=false" in read(
        "platform/live-lab/manifests/app/patches/patch-ingress-live-lab.yaml"
    )


def test_waf_defaults_to_count_and_uses_scoped_custom_header_drill():
    variables = read("platform/live-lab/terraform/variables.tf")
    main = read("platform/live-lab/terraform/main.tf")

    waf_mode = re.search(r'variable "waf_enforcement_mode" \{(?P<body>.*?)\n\}', variables, re.DOTALL)
    assert waf_mode is not None
    assert 'default     = "COUNT"' in waf_mode.group("body")
    assert "single_header" in main
    assert 'name = "x-live-lab-waf-drill"' in main
    assert "AWSManagedRulesKnownBadInputsRuleSet" not in main
    assert "harmful" not in main.lower()
    assert 'var.waf_enforcement_mode == "BLOCK"' in main


def test_public_readme_and_runtime_artifacts_document_operational_boundaries():
    readme = read("README.md")
    acceptance = read("platform/live-lab/acceptance.yaml")
    source_hash = read("platform/live-lab/scripts/source_hash.sh")
    variables = read("platform/live-lab/terraform/variables.tf")
    terraform = read("platform/live-lab/terraform/main.tf")
    estimator = read("platform/live-lab/scripts/estimate_session_cost.py")

    assert "승인된 비용 계획을 확인하고 종료·정리 절차를 준비해야 합니다" in readme
    assert "비용 추정은 AWS가 강제하는 지출 상한이 아니며" in readme
    assert "실환경 실행 증거가 아닙니다" in readme
    assert "원본 실행 기록, 개인 작업 기록, 자격 증명은 Git 추적에서 제외해 로컬에만 보관합니다" in readme
    assert "Local kubectl rendering is not GitOps or cloud execution evidence." in acceptance
    assert "git diff --binary HEAD" in source_hash
    assert "tracked_diff_hash" in source_hash
    assert "untracked_hash" in source_hash
    assert 'variable "aws_region"' in variables
    region_block = variables.split('variable "aws_region" {', 1)[1].split("\n}", 1)[0]
    assert "Explicit AWS region approved for this isolated live lab" in region_block
    assert "default" not in region_block
    assert 'variable "cost_budget_usd"' in variables
    assert 'variable "max_session_hours"' in variables
    assert "estimate_session_cost.py" in readme
    assert '"within_budget"' in estimator
    assert "ephemeral" in terraform.lower()
    assert 'resource "aws_db_instance" "mysql_primary"' in terraform
    assert 'multi_az               = true' in terraform
    assert 'resource "aws_db_instance" "mysql_reader"' in terraform
    assert "replicate_source_db" in terraform
    assert "AWS Load Balancer Controller" in terraform
    assert "aws_nat_gateway" not in terraform
    assert "aws_bastion_host" not in terraform
    assert "수만 동시 사용자" not in readme
    assert "lane E" not in readme
    assert "leader" not in readme.lower()
    assert "Kyobo" not in readme


def test_node_default_can_schedule_documented_eks_request_floor():
    variables = read("platform/live-lab/terraform/variables.tf")
    example = read("platform/live-lab/terraform/example.auto.tfvars.example")
    configuration = variables + example

    assert 'default     = "m7i.xlarge"' in variables
    assert 'eks_node_instance_type = "m7i.xlarge"' in example
    assert 'condition     = var.eks_node_min_size == 2' in variables
    assert 'condition     = var.eks_node_desired_size == 2' in variables
    assert 'condition     = var.eks_node_max_size == 2' in variables
    assert "eks_node_max_size      = 2" in example
    assert "m7i.large" not in configuration


def test_cost_estimator_stays_under_approved_ceiling_and_fails_closed():
    script = LIVE_LAB / "scripts/estimate_session_cost.py"
    result = subprocess.run(
        ["python3", str(script), "--hours", "3", "--requests", "40000", "--budget", "5.50", "--reserve", "1.00"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    estimate = json.loads(result.stdout)
    assert estimate["region"] == "ap-northeast-2"
    assert estimate["base_estimate_usd"] == 4.0594
    assert estimate["planning_total_usd"] == 5.0594
    assert estimate["within_budget"] is True

    over_budget = subprocess.run(
        ["python3", str(script), "--hours", "3", "--requests", "40000", "--budget", "4.5", "--reserve", "1.00"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert over_budget.returncode == 2
    assert json.loads(over_budget.stdout)["within_budget"] is False


def test_operator_cidr_preparation_rejects_non_global_addresses():
    script = read("platform/live-lab/scripts/prepare_session.sh")
    assert "network.network_address.is_global" in script
    assert "globally routable public IPv4 /32" in script


def test_manifest_split_keeps_argocd_app_inside_governance_boundary():
    root_kustomization = read("platform/live-lab/manifests/kustomization.yaml")
    app_kustomization = read("platform/live-lab/manifests/app/kustomization.yaml")
    bootstrap_kustomization = read("platform/live-lab/manifests/bootstrap/kustomization.yaml")
    config = read("platform/live-lab/manifests/bootstrap/resources/raffle-config.yaml")

    assert "resources:\n  - app" in root_kustomization
    assert "namespace: platform-validation" in app_kustomization
    assert "- ../../../../k8s/base" in app_kustomization
    assert "../patch-" not in app_kustomization
    assert "../raffle-config.yaml" not in app_kustomization
    assert not (LIVE_LAB / "manifests/app/resources").exists()
    assert "namespace.yaml" not in app_kustomization
    assert "raffle-secret" not in app_kustomization
    assert "mysql.yaml" not in app_kustomization
    assert "resources/namespace.yaml" in bootstrap_kustomization
    assert "resources/raffle-config.yaml" in bootstrap_kustomization
    assert "../../secrets" not in bootstrap_kustomization
    assert not (LIVE_LAB / "manifests/bootstrap/resources/mysql.yaml").exists()
    assert "__DB_WRITER_HOST__" in config
    assert "__DB_READER_HOST__" in config
    assert "DB_REQUIRE_TLS: \"true\"" in config
    assert "DB_SSL_CA: /etc/rds-ca/global-bundle.pem" in config
    assert not (LIVE_LAB / "manifests/raffle-secret.example.yaml").exists()


def test_rendered_live_rollout_preserves_base_container_and_uses_separate_migration_secret():
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(LIVE_LAB / "manifests" / "app")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rendered.returncode == 0, rendered.stderr
    documents = list(yaml.safe_load_all(rendered.stdout))
    rollout = next(item for item in documents if item and item.get("kind") == "Rollout")
    container = rollout["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "data-pipeline-app:latest"
    assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
    assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert {item["secretRef"]["name"] for item in container["envFrom"] if "secretRef" in item} == {"raffle-secret"}
    assert container["volumeMounts"][0]["mountPath"] == "/etc/rds-ca"

    migration = next(item for item in documents if item and item.get("kind") == "Job")
    assert migration["metadata"]["labels"]["live-lab-session"] == "__SESSION_ID__"
    assert migration["metadata"]["labels"]["live-lab-approval"] == "__APPROVAL_ID__"
    migration_container = migration["spec"]["template"]["spec"]["containers"][0]
    migration_secrets = {
        item["secretRef"]["name"] for item in migration_container["envFrom"] if "secretRef" in item
    }
    assert "raffle-migration-secret" in migration_secrets
    migration_env = {item["name"]: item["value"] for item in migration_container["env"]}
    assert migration_env["SEED_SAMPLE_DATA"] == "true"
    assert migration_env["LIVE_LAB_SYNTHETIC_SEED"] == "true"


def test_migration_patch_has_full_security_context_not_only_partial_scanner_patch():
    patch = read("platform/live-lab/manifests/app/patches/patch-migration-live-lab.yaml")

    assert "automountServiceAccountToken: false" in patch
    assert "runAsNonRoot: true" in patch
    assert "seccompProfile:" in patch
    assert "allowPrivilegeEscalation: false" in patch
    assert "readOnlyRootFilesystem: true" in patch
    assert "capabilities:" in patch
    assert "drop: [\"ALL\"]" in patch
    assert "PYTHONDONTWRITEBYTECODE" in patch
    assert "emptyDir: {}" in patch


def test_runtime_renderer_never_needs_a_local_secret_file_and_renders_session_endpoints():
    renderer = read("platform/live-lab/scripts/render_runtime.sh")
    gitignore = read("platform/live-lab/.gitignore")

    assert "render_runtime.sh [--static]" in renderer
    assert "umask 077" in renderer
    assert "mktemp -d platform/live-lab/evidence/.render.XXXXXX" in renderer
    assert "/bin/mv" in renderer
    assert "chmod 600" in renderer
    assert "DB_WRITER_HOST" in renderer and "DB_READER_HOST" in renderer
    assert "ALB_CERTIFICATE_ARN" in renderer
    assert "WAF_WEB_ACL_ARN" in renderer
    assert "raffle-secret.yaml" not in renderer
    secret_script = read("platform/live-lab/scripts/create_session_secrets.sh")
    assert "--secret-string file:///dev/stdin" in secret_script
    assert "kubectl" not in secret_script
    assert "manifests/bootstrap" in renderer
    assert "manifests/app" in renderer
    assert "secrets/*" in gitignore
    assert "evidence/*" in gitignore
    assert "!evidence/schema.json" in gitignore
    assert "!secrets/README.md" not in gitignore
    assert "!evidence/README.md" not in gitignore


def test_secrets_are_staged_around_terraform_and_scoped_to_exact_aws_and_eks_identity():
    master_script = read("platform/live-lab/scripts/create_session_secrets.sh")
    runtime_script = read("platform/live-lab/scripts/create_runtime_k8s_secret.sh")

    assert "ResourceNotFoundException" in master_script
    assert "file:///dev/stdin" in master_script
    assert "Key=Operation,Value=$OPERATION_ID" in master_script
    assert "kubectl" not in master_script
    assert "get-secret-value" in runtime_script
    assert "describe-cluster" in runtime_script
    assert "cluster_endpoint" in runtime_script
    assert "--context \"$KUBE_CONTEXT\"" in runtime_script
    assert '"live-lab-session": os.environ["LIVE_LAB_SESSION"]' in runtime_script
    assert '"live-lab-approval": os.environ["LIVE_LAB_APPROVAL"]' in runtime_script
    assert "python3 - <<'PY'" in runtime_script
    assert 'MIGRATION_SECRET_NAME="${K8S_MIGRATION_SECRET_NAME:-raffle-migration-secret}"' in runtime_script
    assert "--labels=" not in runtime_script
    assert "--from-literal" not in runtime_script
    assert '"DB_ADMIN_USER": os.environ["LIVE_LAB_DB_ADMIN_USER"]' in runtime_script
    assert "runtime secrets already exist; inspect them, do not overwrite them." in runtime_script


def test_runtime_renderer_exercises_bootstrap_app_boundary_and_private_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    shutil.copytree(REPO_ROOT / "k8s", repo / "k8s")
    lab = repo / "platform/live-lab"
    shutil.copytree(LIVE_LAB / "manifests", lab / "manifests")
    shutil.copytree(LIVE_LAB / "scripts", lab / "scripts")
    command = ["bash", "platform/live-lab/scripts/render_runtime.sh"]
    test_env = {
        **os.environ,
        "DB_WRITER_HOST": "writer.internal",
        "DB_READER_HOST": "reader.internal",
        "ALB_CERTIFICATE_ARN": "arn:aws:acm:ap-northeast-2:123456789012:certificate/01234567-abcd-0123-abcd-0123456789ab",
        "WAF_WEB_ACL_ARN": "arn:aws:wafv2:ap-northeast-2:123456789012:regional/webacl/kyobo-lab/01234567-abcd-0123-abcd-0123456789ab",
        "OPERATOR_CIDR": "203.0.113.10/32",
        "SESSION_ID": "kyobo-lab-0001",
        "APPROVAL_ID": "SS0-20260923-test",
        "ECR_REPOSITORY_URL": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/kyobo-kyobo-lab-0001/data-pipeline-app",
        "APP_IMAGE_REF": "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/kyobo-kyobo-lab-0001/data-pipeline-app@sha256:" + "a" * 64,
    }
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, env=test_env)
    assert result.returncode == 0, result.stderr
    app = list(yaml.safe_load_all((lab / "evidence/rendered-app-manifests.yaml").read_text()))
    assert not {"Secret", "Namespace", "Deployment", "Role", "RoleBinding", "ServiceAccount"}.intersection(d["kind"] for d in app if d)
    ingress = next(d for d in app if d and d["kind"] == "Ingress")
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/certificate-arn"] == test_env["ALB_CERTIFICATE_ARN"]
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/wafv2-acl-arn"] == test_env["WAF_WEB_ACL_ARN"]
    assert ingress["metadata"]["annotations"]["alb.ingress.kubernetes.io/tags"].endswith("Session=kyobo-lab-0001,Approval=SS0-20260923-test,ManagedBy=terraform")
    config = next(d for d in yaml.safe_load_all((lab / "evidence/rendered-bootstrap-manifests.yaml").read_text()) if d and d["kind"] == "ConfigMap")
    assert config["data"]["DB_WRITER_HOST"] == "writer.internal"
    assert config["data"]["DB_READER_HOST"] == "reader.internal"
    for artifact in (lab / "evidence").glob("rendered-*.yaml"):
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    bootstrap = list(yaml.safe_load_all((lab / "evidence/rendered-bootstrap-manifests.yaml").read_text()))
    assert {"Namespace", "ConfigMap"}.issubset(d["kind"] for d in bootstrap if d)
    bootstrap_objects = {(d["kind"], d["metadata"]["name"]) for d in bootstrap if d}
    for job in (d for d in app if d and d["kind"] == "Job" and d["metadata"].get("annotations", {}).get("argocd.argoproj.io/hook") == "PreSync"):
        for container in job["spec"]["template"]["spec"]["containers"]:
            for reference in container.get("envFrom", []):
                for key, kind in (("configMapRef", "ConfigMap"), ("secretRef", "Secret")):
                    if key in reference:
                        if kind == "Secret":
                            assert reference[key]["name"] == "raffle-migration-secret"
                        else:
                            assert (kind, reference[key]["name"]) in bootstrap_objects


def parse_source_hash(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.strip().splitlines())


def test_source_hash_is_stable_across_repeated_runs_and_includes_staged_changes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)

    script_dir = repo / "platform" / "live-lab" / "scripts"
    script_dir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "platform/live-lab/scripts/source_hash.sh", script_dir / "source_hash.sh")
    shutil.copy2(REPO_ROOT / "platform/live-lab/.gitignore", repo / "platform/live-lab/.gitignore")

    (repo / "tracked.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

    first = subprocess.run(
        ["bash", "platform/live-lab/scripts/source_hash.sh"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    second = subprocess.run(
        ["bash", "platform/live-lab/scripts/source_hash.sh"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert first == second
    assert (repo / "platform/live-lab/evidence/source-hash.txt").is_file()
    subprocess.run(
        ["git", "check-ignore", "platform/live-lab/evidence/source-hash.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )

    (repo / "tracked.txt").write_text("v2\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    staged = subprocess.run(
        ["bash", "platform/live-lab/scripts/source_hash.sh"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert parse_source_hash(staged)["tracked_diff_hash"] != parse_source_hash(first)["tracked_diff_hash"]


def test_teardown_cleans_private_temp_directory_on_failure_and_blocks_unowned_state():
    teardown = read("platform/live-lab/scripts/teardown_live_lab.sh")
    lifecycle = read("platform/live-lab/scripts/live_lab_lifecycle.py")

    assert '[[ -z "$tmp_dir" ]] || rm -rf -- "$tmp_dir"' in teardown
    assert 'elif [[ -e "$repo_root/$TERRAFORM_DIR/terraform.tfstate.backup" ]]' in teardown
    assert 'validate-state "$tmp_dir/state.json" --session "$SESSION_ID" --approval "$APPROVAL_ID"' in teardown
    assert '"terraform_data"' in lifecycle
    assert 'state resource ownership mismatch' in lifecycle
    assert 'untagged state resource needs manual review' in lifecycle


def test_teardown_skips_destroy_plan_when_local_state_file_has_no_resources():
    teardown = read("platform/live-lab/scripts/teardown_live_lab.sh")

    assert 'managed_state="$(terraform -chdir="$TERRAFORM_DIR" state list)"' in teardown
    assert 'if [[ -n "$managed_state" ]]; then' in teardown
    assert 'Terraform state is already empty; skip destroy plan.' in teardown


def test_teardown_skips_empty_certificate_arn_lines_and_reconciles_tag_history():
    teardown = read("platform/live-lab/scripts/teardown_live_lab.sh")

    assert '[[ -n "$cert_arn" ]] || continue' in teardown
    assert 'reconcile-inventory' in teardown


def test_rds_ca_and_deployment_scripts_never_use_ambient_kubectl_context():
    ca_script = read("platform/live-lab/scripts/prepare_rds_ca_bundle.sh")
    deploy_script = read("platform/live-lab/scripts/deploy_live_lab.sh")

    assert ': "${KUBE_CONTEXT:?KUBE_CONTEXT must point to the exact session EKS cluster}"' in ca_script
    assert 'kube() { kubectl --context "$KUBE_CONTEXT" "$@"; }' in ca_script
    assert 'kubectl --context "$KUBE_CONTEXT" config view --minify' in ca_script
    assert "kubectl get configmap" not in ca_script
    assert 'kubectl --context "$KUBE_CONTEXT" config view --minify' in deploy_script
    assert "wait --for=condition=complete job/data-pipeline-schema-migration" in deploy_script
    assert 'apply --filename -' in deploy_script
    assert 'item.get("metadata", {}).get("name") == "data-pipeline-schema-migration"' in deploy_script


def test_lifecycle_state_gate_accepts_only_exact_session_tags(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(
        __import__("json").dumps({
            "values": {
                "root_module": {
                    "resources": [{
                        "address": "aws_vpc.lab",
                        "mode": "managed",
                        "type": "aws_vpc",
                        "values": {"tags_all": {
                            "Project": "kyobo-platform-live-lab",
                            "Session": "live-260923-a1b2c3",
                            "Approval": "SS0-20260923-codex-live-lab",
                        }},
                    }, {
                        "address": "terraform_data.approval_gate",
                        "mode": "managed",
                        "type": "terraform_data",
                        "values": {"input": {
                            "session_id": "live-260923-a1b2c3",
                            "approval_id": "SS0-20260923-codex-live-lab",
                        }},
                    }],
                },
            },
        }),
        encoding="utf-8",
    )
    command = [
        "python3", str(LIVE_LAB / "scripts/live_lab_lifecycle.py"), "validate-state",
        str(state_file), "--session", "live-260923-a1b2c3", "--approval", "SS0-20260923-codex-live-lab",
    ]
    good = subprocess.run(command, check=False, capture_output=True, text=True)
    assert good.returncode == 0, good.stderr
    assert "validated 2" in good.stdout

    document = __import__("json").loads(state_file.read_text(encoding="utf-8"))
    document["values"]["root_module"]["resources"][0]["values"]["tags_all"]["Session"] = "some-other-session"
    state_file.write_text(__import__("json").dumps(document), encoding="utf-8")
    bad = subprocess.run(command, check=False, capture_output=True, text=True)
    assert bad.returncode == 2
    assert "ownership mismatch" in bad.stderr


def test_teardown_confirmation_guard_runs_before_any_aws_command(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "platform/live-lab/scripts").mkdir(parents=True)
    (repo / "platform/live-lab/evidence").mkdir(parents=True)
    shutil.copy2(LIVE_LAB / "scripts/teardown_live_lab.sh", repo / "platform/live-lab/scripts/teardown_live_lab.sh")
    shutil.copy2(LIVE_LAB / "scripts/live_lab_lifecycle.py", repo / "platform/live-lab/scripts/live_lab_lifecycle.py")
    (repo / "platform/live-lab/evidence/session.auto.tfvars.json").write_text("{}\n", encoding="utf-8")
    os.chmod(repo / "platform/live-lab/evidence/session.auto.tfvars.json", 0o600)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    aws_marker = tmp_path / "aws-was-called"
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(f"#!/bin/sh\ntouch '{aws_marker}'\nexit 99\n", encoding="utf-8")
    os.chmod(fake_aws, 0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "AWS_PROFILE": "fake",
        "AWS_REGION": "ap-northeast-2",
        "EXPECTED_ACCOUNT_ID": "123456789012",
        "SESSION_ID": "live-260923-a1b2c3",
        "APPROVAL_ID": "SS0-20260923-codex-live-lab",
        "KUBE_CONTEXT": "unused",
        "LIVE_LAB_TFVARS": "platform/live-lab/evidence/session.auto.tfvars.json",
    }
    result = subprocess.run(
        ["bash", "platform/live-lab/scripts/teardown_live_lab.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "TEARDOWN_CONFIRM" in result.stderr
    assert not aws_marker.exists()


def test_rds_recovery_scope_cannot_be_retargeted_to_another_session():
    script = read("scripts/rds-failover-drill.sh")
    assert '"$RESOURCE_PREFIX" == "kyobo-${SESSION_ID}"' in script
    assert "validate_identifiers" in script


def test_all_live_aws_scripts_ignore_ambient_static_credentials():
    scripts = [
        "prepare_session.sh",
        "create_session_secrets.sh",
        "import_session_acm_certificate.sh",
        "bootstrap_cluster.sh",
        "create_runtime_k8s_secret.sh",
        "prepare_rds_ca_bundle.sh",
        "deploy_live_lab.sh",
        "build_push_image.sh",
        "teardown_live_lab.sh",
        "deadline_watchdog.sh",
    ]
    for name in scripts:
        source = read(f"platform/live-lab/scripts/{name}")
        unset_at = source.find("unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN")
        aws_at = source.find("aws --profile")
        assert unset_at >= 0 and aws_at > unset_at, name

    drill = read("scripts/rds-failover-drill.sh")
    assert drill.find("unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN") < drill.find("aws --profile")


def test_deadline_watchdog_validates_private_session_scope_and_stays_local():
    script = read("platform/live-lab/scripts/deadline_watchdog.sh")
    assert "caffeinate -dimsu" in script
    assert "AWS cleanup retries exhausted" in script or "deadline cleanup retries exhausted" in script
    assert '"$tfvars_mode" == 600' in script
    assert "protected tfvars identity does not match watchdog scope" in script
    assert 'watchdog_pid="$$"' in script
    assert "Keep this managed terminal session alive." in script
    assert "trap on_exit EXIT" in script
    assert "trap 'exit 143' TERM" in script
    assert "nohup" not in script
    assert "if bash platform/live-lab/scripts/teardown_live_lab.sh; then" in script
    assert "AWS hard spending limit" not in script


def test_argo_rollouts_large_crds_use_server_side_apply():
    script = read("platform/live-lab/scripts/bootstrap_cluster.sh")
    assert 'kubectl --context "$KUBE_CONTEXT" apply --server-side --field-manager=live-lab-bootstrap --namespace argo-rollouts -f "$install_file"' in script


def test_bootstrap_helm_installs_use_verified_context():
    script = read("platform/live-lab/scripts/bootstrap_cluster.sh")
    install_lines = [line for line in script.splitlines() if "upgrade --install" in line]
    assert len(install_lines) == 2
    assert all('helm --kube-context "$KUBE_CONTEXT"' in line for line in install_lines)


def test_failed_session_migration_can_retry_only_after_job_is_inactive_and_owned():
    script = read("platform/live-lab/scripts/deploy_live_lab.sh")
    assert 'labels.get("live-lab-session") != sys.argv[2]' in script
    assert 'labels.get("live-lab-approval") != sys.argv[3]' in script
    assert 'failed > 0 and active == 0' in script
    assert 'delete job data-pipeline-schema-migration --wait=true' in script
    assert "schema migration" in script and "idempotent" in script




def test_plan_validator_rejects_cost_shape_drift_before_apply(tmp_path):
    session = "live-260923-a1b2c3"
    approval = "SS0-20260923-codex-live-lab"
    tags = {"Project": "kyobo-platform-live-lab", "Session": session, "Approval": approval}
    resources = [
        {"address": "aws_eks_cluster.lab", "mode": "managed", "type": "aws_eks_cluster", "values": {"tags_all": tags}},
        {"address": "aws_eks_node_group.lab", "mode": "managed", "type": "aws_eks_node_group", "values": {"tags_all": tags, "instance_types": ["m7i.xlarge"], "scaling_config": [{"min_size": 2, "desired_size": 2, "max_size": 2}]}},
        {"address": "aws_db_instance.mysql_primary", "mode": "managed", "type": "aws_db_instance", "values": {"tags_all": tags, "instance_class": "db.t3.small", "multi_az": True, "publicly_accessible": False, "allocated_storage": 20, "storage_type": "gp3"}},
        {"address": "aws_db_instance.mysql_reader", "mode": "managed", "type": "aws_db_instance", "values": {"tags_all": tags, "instance_class": "db.t3.small", "multi_az": False, "publicly_accessible": False}},
        {"address": "aws_wafv2_web_acl.lab", "mode": "managed", "type": "aws_wafv2_web_acl", "values": {"tags_all": tags}},
        {"address": "aws_ecr_repository.app", "mode": "managed", "type": "aws_ecr_repository", "values": {"tags_all": tags}},
    ]
    variables = {
        "aws_account_id": "123456789012",
        "aws_region": "ap-northeast-2",
        "session_id": session,
        "approval_id": approval,
        "cost_budget_usd": 5.5,
        "max_session_hours": 3,
        "apply_approval_phrase": "APPROVED_FOR_EPHEMERAL_APPLY",
        "eks_node_instance_type": "m7i.xlarge",
        "eks_node_min_size": 2,
        "eks_node_desired_size": 2,
        "eks_node_max_size": 2,
    }
    plan = {
        "variables": {key: {"value": value} for key, value in variables.items()},
        "planned_values": {"root_module": {"resources": resources}},
        "resource_changes": [{"address": item["address"], "change": {"actions": ["create"]}} for item in resources],
        "configuration": {"root_module": {"resources": [{
            "address": "aws_db_instance.mysql_reader",
            "expressions": {"replicate_source_db": {"references": ["aws_db_instance.mysql_primary.arn"]}},
        }]}},
    }
    resources[1]["values"]["disk_size"] = 20
    resources[2]["values"].update(engine="mysql", engine_version="8.4")
    for db in resources[2:4]:
        db["values"]["engine_lifecycle_support"] = "open-source-rds-extended-support-disabled"
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    command = [
        "python3", str(LIVE_LAB / "scripts/validate_session_plan.py"), str(plan_file),
        "--account", "123456789012", "--region", "ap-northeast-2", "--session", session, "--approval", approval,
    ]
    valid = subprocess.run(command, check=False, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
    assert "Validated 6 AWS plan resources" in valid.stdout

    for index, key, bad in [(1, "disk_size", 50), (2, "engine_version", "8.0"),
                             (2, "engine_lifecycle_support", "open-source-rds-extended-support"),
                             (3, "engine_lifecycle_support", None)]:
        original = resources[index]["values"][key]
        resources[index]["values"][key] = bad
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        blocked = subprocess.run(command, check=False, capture_output=True, text=True)
        assert blocked.returncode == 2
        resources[index]["values"][key] = original
    plan["variables"]["max_session_hours"]["value"] = 6
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    assert subprocess.run(command, capture_output=True).returncode == 2
    plan["variables"]["max_session_hours"]["value"] = 3

    plan["planned_values"]["root_module"]["resources"][1]["values"]["scaling_config"][0]["max_size"] = 3
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    invalid = subprocess.run(command, check=False, capture_output=True, text=True)
    assert invalid.returncode == 2
    assert "fixed at exactly two nodes" in invalid.stderr

    plan["planned_values"]["root_module"]["resources"][1]["values"]["scaling_config"][0]["max_size"] = 2
    unreviewed = dict(plan["planned_values"]["root_module"]["resources"][0])
    unreviewed.update(address="aws_cloudwatch_metric_alarm.extra", type="aws_cloudwatch_metric_alarm")
    plan["planned_values"]["root_module"]["resources"].append(unreviewed)
    plan["resource_changes"].append({"address": unreviewed["address"], "change": {"actions": ["create"]}})
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    rejected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert rejected.returncode == 2
    assert "unreviewed AWS resource types" in rejected.stderr
