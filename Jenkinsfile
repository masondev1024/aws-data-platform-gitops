pipeline {
  agent {
    label 'platform-agent'
  }

  options {
    buildDiscarder(logRotator(numToKeepStr: '20'))
    disableConcurrentBuilds()
    skipDefaultCheckout(true)
    timeout(time: 30, unit: 'MINUTES')
    timestamps()
  }

  environment {
    PIP_DISABLE_PIP_VERSION_CHECK = '1'
    PIP_NO_INPUT = '1'
    PLATFORM_VERIFY_PHASES = 'tests manifests terraform python-security'
    PYTHON_VENV = '.venv'
  }

  parameters {
    string(name: 'LOCAL_SOURCE_SHA256', defaultValue: '', description: 'Trusted local archive SHA-256; blank uses SCM checkout.')
    booleanParam(name: 'FAILURE_DRILL', defaultValue: false, description: 'Local snapshot only: inject one failing pytest to verify failure propagation.')
  }

  stages {
    stage('Checkout trusted source') {
      steps {
        script {
          if (params.LOCAL_SOURCE_SHA256) {
            if (!(params.LOCAL_SOURCE_SHA256 ==~ /[0-9a-f]{64}/)) {
              error('LOCAL_SOURCE_SHA256 must be a lowercase SHA-256')
            }
            deleteDir()
            sh '''#!/usr/bin/env bash
              set -euo pipefail
              archive="/tmp/jenkins-source-${LOCAL_SOURCE_SHA256}.tar.gz"
              printf '%s  %s\\n' "${LOCAL_SOURCE_SHA256}" "${archive}" | sha256sum -c -
              tar -xzf "${archive}"
              mkdir -p .jenkins-artifacts
              printf '%s\\n' "${LOCAL_SOURCE_SHA256}" > .jenkins-artifacts/source-sha256.log
              sha256sum -c .jenkins-source-files.sha256 > .jenkins-artifacts/source-files.log
            '''
          } else {
            if (params.FAILURE_DRILL) {
              error('FAILURE_DRILL requires a trusted local snapshot')
            }
            checkout scm
          }
          if (params.FAILURE_DRILL) {
            writeFile file: 'app/tests/test_jenkins_failure_drill.py', text: '''def test_intentional_jenkins_failure():
    assert False, "JENKINS_INTENTIONAL_FAILURE: verify pytest exit propagation"
'''
          }
        }
      }
    }

    stage('Prepare verifier toolchain') {
      steps {
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          python3 -m venv "${PYTHON_VENV}"
          . "${PYTHON_VENV}/bin/activate"
          python -m pip install -r app/requirements-dev.txt

          python --version
          pytest --version
          bandit --version
          pip-audit --version
          kubectl version --client=true
          terraform version
        '''
      }
    }

    stage('Run approved Jenkins subset') {
      steps {
        sh '''#!/usr/bin/env bash
          set -euo pipefail
          . "${PYTHON_VENV}/bin/activate"
          mkdir -p .jenkins-artifacts

          for phase in ${PLATFORM_VERIFY_PHASES}; do
            case "${phase}" in
              tests|manifests|terraform|python-security) ;;
              *) echo "Unsupported Jenkins verification phase: ${phase}" >&2; exit 64 ;;
            esac

            echo "==> scripts/verify_platform.sh ${phase}"
            bash scripts/verify_platform.sh "${phase}" 2>&1 | tee ".jenkins-artifacts/${phase}.log"
          done
        '''
      }
    }
  }

  post {
    always {
      archiveArtifacts artifacts: '.jenkins-artifacts/**/*.log', allowEmptyArchive: true, fingerprint: true
      cleanWs(deleteDirs: true, disableDeferredWipeout: true)
    }
  }
}
