# 1. 파이썬 실행 환경 선택 (가벼운 버전인 alpine이나 slim 추천)
FROM python:3.9-slim

# 2. 컨테이너 내부 작업 디렉토리 설정
WORKDIR /app

# 3. 필요한 라이브러리 설치 파일 복사 (requirements.txt가 있다면)
# 없다면 이 단계는 생략해도 되지만, 보통 아래처럼 작성해.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 내 소스 코드와 템플릿 폴더 복사
# k8s 폴더는 복사하지 않도록 명시적으로 필요한 것만 골라 담자.
COPY app.py .
COPY draw_winner.py .
COPY templates/ ./templates/

# 5. Flask 서버 실행 (8080 포트 사용 가정)
EXPOSE 8080
CMD ["python", "app.py"]
