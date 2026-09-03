# 1. AWS Load Balancer Controller용 IAM Role (IRSA)
module "lb_role" {
  source                                 = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version                                = "~> 5.0"
  role_name                              = "eks-alb-controller-role"
  attach_load_balancer_controller_policy = true
  oidc_providers = {
    main = {
      provider_arn               = aws_iam_openid_connect_provider.eks.arn
      namespace_service_accounts = ["kube-system:aws-load-balancer-controller"]
    }
  }
}

# 2. ALB Controller 설치 (Helm)
resource "helm_release" "alb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  namespace  = "kube-system"

  set {
    name  = "clusterName"
    value = aws_eks_cluster.main.name
  }

  # ALB Controller가 private node에서 IMDS로 VPC ID를 찾지 못해 CrashLoop하지
  # 않도록 IaC에서 계정/클러스터 좌표를 명시한다.
  set {
    name  = "region"
    value = var.aws_region
  }

  set {
    name  = "vpcId"
    value = aws_vpc.main.id
  }

  # 단일 t3.medium 검증 노드에서는 webhook 가용성만 필요하므로 replica를 축소한다.
  set {
    name  = "replicaCount"
    value = "1"
  }

  set {
    name  = "serviceAccount.create"
    value = "true"
  }

  set {
    name  = "serviceAccount.name"
    value = "aws-load-balancer-controller"
  }

  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = module.lb_role.iam_role_arn
  }
}

# 3. Argo Rollouts 설치
resource "helm_release" "argo_rollouts" {
  name             = "argo-rollouts"
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-rollouts"
  namespace        = "argo-rollouts"
  create_namespace = true

  set {
    name  = "controller.replicas"
    value = "1"
  }
}

# 4. Metrics Server (HPA 작동 필수)
resource "helm_release" "metrics_server" {
  name       = "metrics-server"
  repository = "https://kubernetes-sigs.github.io/metrics-server/"
  chart      = "metrics-server"
  namespace  = "kube-system"
}

# 5. Prometheus, Grafana, and Alertmanager.
# The chart is pinned so a future chart release cannot silently change
# scrape, alerting, or CRD behavior during an infrastructure apply.
resource "helm_release" "kube_prometheus_stack" {
  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = "88.5.4"
  namespace        = "monitoring"
  create_namespace = true
  atomic           = true
  cleanup_on_fail  = true
  timeout          = 900

  values = [yamlencode({
    crds = {
      enabled = true
    }
    grafana = {
      enabled = true
      service = {
        type = "ClusterIP"
      }
      persistence = {
        enabled = false
      }
      resources = {
        requests = {
          cpu    = "100m"
          memory = "128Mi"
        }
        limits = {
          cpu    = "300m"
          memory = "512Mi"
        }
      }
      sidecar = {
        dashboards = {
          enabled         = true
          searchNamespace = "default"
        }
      }
    }
    prometheus = {
      enabled = true
      service = {
        type = "ClusterIP"
      }
      prometheusSpec = {
        retention                               = "7d"
        scrapeInterval                          = "15s"
        evaluationInterval                      = "15s"
        serviceMonitorSelectorNilUsesHelmValues = false
        serviceMonitorSelector = {
          matchLabels = {
            monitoring = "data-pipeline"
          }
        }
        serviceMonitorNamespaceSelector = {}
        ruleSelectorNilUsesHelmValues   = false
        ruleSelector = {
          matchLabels = {
            monitoring = "data-pipeline"
          }
        }
        ruleNamespaceSelector = {}
        resources = {
          requests = {
            cpu    = "250m"
            memory = "512Mi"
          }
          limits = {
            cpu    = "750m"
            memory = "1Gi"
          }
        }
      }
    }
    alertmanager = {
      enabled = true
      alertmanagerSpec = {
        retention = "72h"
        resources = {
          requests = {
            cpu    = "100m"
            memory = "128Mi"
          }
          limits = {
            cpu    = "300m"
            memory = "256Mi"
          }
        }
      }
      config = {
        route = {
          receiver        = "generic-webhook"
          group_by        = ["alertname", "service"]
          group_wait      = "30s"
          group_interval  = "5m"
          repeat_interval = "4h"
        }
        receivers = [{
          # kube-prometheus-stack adds a default Watchdog route that targets
          # this receiver. Keep it even when supplying a custom receiver list.
          name = "null"
          }, {
          name = "generic-webhook"
          webhook_configs = [{
            url           = var.alertmanager_webhook_url
            send_resolved = true
          }]
        }]
      }
    }
  })]

  depends_on = [
    helm_release.argo_rollouts,
    helm_release.metrics_server,
  ]
}
