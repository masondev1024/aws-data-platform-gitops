from datetime import datetime
import os
from time import perf_counter

import pymysql
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

app = Flask(__name__)
configured_secret_key = os.environ.get('SECRET_KEY')
if os.environ.get('FLASK_ENV', '').lower() == 'production' and not configured_secret_key:
    raise RuntimeError('SECRET_KEY must be configured in production')
app.secret_key = configured_secret_key or 'dev-only-secret-key'

# 1. ConfigMap에서 가져오기
DB_WRITER_HOST = os.environ.get('DB_WRITER_HOST')
DB_READER_HOST = os.environ.get('DB_READER_HOST')
DB_NAME = os.environ.get('DB_NAME', 'raffle_db')
DB_USER = os.environ.get('DB_USER', 'admin')
DB_PASSWORD = os.environ.get('DB_PASSWORD')

HTTP_REQUESTS = Counter(
    'raffle_http_requests',
    'Total HTTP requests handled by the raffle application.',
    ('method', 'route', 'status'),
)
HTTP_REQUEST_DURATION = Histogram(
    'raffle_http_request_duration_seconds',
    'HTTP request duration in seconds.',
    ('method', 'route'),
)
RAFFLE_APPLY_REQUESTS = Counter(
    'raffle_apply_requests',
    'Raffle application outcomes.',
    ('result',),
)
DB_READINESS = Gauge(
    'raffle_db_readiness',
    'Whether the configured read database passed the latest readiness check.',
)


def _metric_route():
    """Return a bounded route label instead of a user-controlled URL path."""
    if request.url_rule is not None:
        return request.url_rule.rule
    return 'unmatched'


@app.before_request
def start_request_timer():
    g.request_started_at = perf_counter()


@app.after_request
def record_request_metrics(response):
    # Scraping /metrics must not create an unbounded self-referential counter.
    if request.endpoint != 'metrics':
        route = _metric_route()
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_REQUEST_DURATION.labels(request.method, route).observe(
            perf_counter() - g.get('request_started_at', perf_counter())
        )
    return response

def get_db_connection(is_write=False):
    """트래픽 분산의 핵심: 쓰기 요청은 Master로, 읽기 요청은 Replica로 연결"""
    host = DB_WRITER_HOST if is_write else DB_READER_HOST
    return pymysql.connect(
        host=host,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        cursorclass=pymysql.cursors.DictCursor # 결과를 딕셔너리 형태로 받아옴
    )


@app.route('/healthz')
def healthz():
    """Process health endpoint used by Kubernetes probes."""
    return jsonify({"status": "ok"})


@app.route('/readyz')
def readyz():
    """Dependency readiness endpoint used before sending user traffic."""
    conn = None
    DB_READINESS.set(0)
    try:
        conn = get_db_connection(is_write=False)
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
    except (pymysql.MySQLError, OSError) as exc:
        app.logger.warning("Database readiness check failed: %s", exc)
        return jsonify({"status": "not_ready"}), 503
    finally:
        if conn is not None:
            conn.close()
    DB_READINESS.set(1)
    return jsonify({"status": "ready"})


@app.route('/metrics')
def metrics():
    """Expose Prometheus metrics for an internal scraper."""
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)

# ==========================================
# 2. 화면 라우팅 (SELECT ➡️ 리플리카 사용)
# ==========================================
@app.route('/')
def index():
    is_logged_in = 'user_id' in session
    
    # [리플리카 DB]에서 상품 목록 가져오기
    conn = get_db_connection(is_write=False)
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM raffle_items ORDER BY end_time ASC")
        items = cursor.fetchall()
    conn.close()

    # datetime 객체를 자바스크립트가 읽기 편한 문자열로 변환
    for item in items:
        if isinstance(item['end_time'], datetime):
            item['end_time'] = item['end_time'].strftime("%Y-%m-%dT%H:%M:%S")

    return render_template('index.html', items=items, is_logged_in=is_logged_in)

@app.route('/login')
def login_page():
    if 'user_id' in session: return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/signup')
def signup_page():
    if 'user_id' in session: return redirect(url_for('index'))
    return render_template('signup.html')

@app.route('/mypage')
def mypage():
    if 'user_id' not in session: return redirect(url_for('login_page'))
    
    current_username = session['user_id']
    conn = get_db_connection(is_write=False)
    with conn.cursor() as cursor:
        sql = """
            SELECT r.title, r.end_time, e.entry_time, r.winner_id, u_winner.username as winner_name, u_me.id as my_id
            FROM raffle_entries e
            JOIN users u_me ON e.user_id = u_me.id
            JOIN raffle_items r ON e.item_id = r.id
            LEFT JOIN users u_winner ON r.winner_id = u_winner.id
            WHERE u_me.username = %s
            ORDER BY e.entry_time DESC
        """
        cursor.execute(sql, (current_username,))
        history_data = cursor.fetchall()
    conn.close()

    my_history = []
    now = datetime.now()
    for row in history_data:
        if row['winner_id'] is None:
            status = "당첨 대기중 ⏳" if now < row['end_time'] else "추첨 진행 중... ⚙️"
        elif row['winner_id'] == row['my_id']:
            status = "축하합니다! 당첨되었습니다! 🎉"
        else:
            status = "다음 기회에..."
            
        my_history.append({
            "title": row['title'],
            "apply_date": row['entry_time'].strftime("%Y-%m-%d %H:%M"),
            "status": status
        })

    return render_template('mypage.html', user_id=current_username, history=my_history)

# ==========================================
# 3. API 라우팅 (INSERT ➡️ 마스터 사용)
# ==========================================
@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    # [마스터 DB] 회원가입 저장
    conn = None
    try:
        conn = get_db_connection(is_write=True)
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, password))
        conn.commit()
        return jsonify({"status": "success", "message": "회원가입 완료! 로그인 페이지로 이동합니다."})
    except pymysql.err.IntegrityError:
        return jsonify({"status": "error", "message": "이미 존재하는 아이디입니다."}), 400
    finally:
        if conn is not None:
            conn.close()

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    # [리플리카 DB] 아이디/비밀번호 확인
    conn = get_db_connection(is_write=False)
    with conn.cursor() as cursor:
        cursor.execute("SELECT id FROM users WHERE username=%s AND password=%s", (username, password))
        user = cursor.fetchone()
    conn.close()

    if user:
        session['user_id'] = username
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "아이디/비밀번호를 확인해주세요."}), 401

@app.route('/api/logout')
def api_logout():
    session.pop('user_id', None)
    return redirect(url_for('index'))

@app.route('/api/apply', methods=['POST'])
def api_apply():
    if 'user_id' not in session:
        RAFFLE_APPLY_REQUESTS.labels('unauthenticated').inc()
        return jsonify({"status": "error", "message": "login_required"}), 401
    
    item_id = request.json.get('item_id')
    current_username = session['user_id']

    conn = None
    try:
        conn = get_db_connection(is_write=True)
        with conn.cursor() as cursor:
            # username으로 user_id(PK) 찾기
            cursor.execute("SELECT id FROM users WHERE username=%s", (current_username,))
            user_pk = cursor.fetchone()['id']
            
            # [마스터 DB] 응모 기록 저장
            cursor.execute("INSERT INTO raffle_entries (user_id, item_id) VALUES (%s, %s)", (user_pk, item_id))
        conn.commit()
        RAFFLE_APPLY_REQUESTS.labels('success').inc()
        return jsonify({"status": "success", "message": "성공적으로 응모되었습니다! 마이페이지에서 확인하세요."})
    except pymysql.err.IntegrityError:
        RAFFLE_APPLY_REQUESTS.labels('duplicate').inc()
        return jsonify({"status": "error", "message": "이미 응모하신 상품입니다!"}), 400
    except pymysql.MySQLError:
        RAFFLE_APPLY_REQUESTS.labels('database_error').inc()
        app.logger.exception("Raffle application failed because of a database error")
        return jsonify({"status": "error", "message": "잠시 후 다시 시도해주세요."}), 503
    finally:
        if conn is not None:
            conn.close()

# ==========================================
# 4. [최초 1회 실행용] DB 테이블 및 초기 데이터 셋업
# ==========================================
@app.route('/init-db')
def initialize_database():
    """빈 DB에 테이블을 만들고 테스트용 래플 상품을 넣는 마법의 URL"""
    # 1. DB 자체(raffle_db)가 없으면 생성
    temp_conn = pymysql.connect(host=DB_WRITER_HOST, user=DB_USER, password=DB_PASSWORD)
    with temp_conn.cursor() as cursor:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    temp_conn.commit()
    temp_conn.close()

    # 2. 테이블 생성 및 샘플 데이터 삽입
    conn = get_db_connection(is_write=True)
    with conn.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(50) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS raffle_items (
                id INT AUTO_INCREMENT PRIMARY KEY,
                title VARCHAR(100) NOT NULL,
                description TEXT,
                end_time DATETIME NOT NULL,
                image_url VARCHAR(255),
                winner_id INT DEFAULT NULL,
                is_drawn BOOLEAN DEFAULT FALSE,
                FOREIGN KEY (winner_id) REFERENCES users(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS raffle_entries (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT,
                item_id INT,
                entry_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (item_id) REFERENCES raffle_items(id),
                UNIQUE KEY unique_entry (user_id, item_id) -- 1인 1회 응모 방지
            )
        """)
        
        # 샘플 래플 상품 데이터 2개 넣기 (테이블이 비어있을 때만)
        cursor.execute("SELECT COUNT(*) as cnt FROM raffle_items")
        if cursor.fetchone()['cnt'] == 0:
            cursor.execute("""
                INSERT INTO raffle_items (title, description, end_time, image_url) VALUES 
                ('나이키 덩크 로우 범고래', '국민 신발, 마지막 기회!', '2026-04-30 18:00:00', 'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=500&q=80'),
                ('애플 에어팟 맥스 실버', '노이즈 캔슬링 끝판왕', '2026-05-05 12:00:00', 'https://images.unsplash.com/photo-1613040809024-b4ef7ba99bc3?w=500&q=80')
            """)
    conn.commit()
    conn.close()
    return "DB 초기화 및 샘플 데이터 삽입 성공! 이제 메인 페이지(/)로 접속하세요."

if __name__ == '__main__':
    debug_enabled = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=8080, debug=debug_enabled)
