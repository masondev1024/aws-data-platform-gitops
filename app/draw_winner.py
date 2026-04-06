import pymysql
import os
import random
from datetime import datetime

# DB 설정 (기존 환경변수 활용)
DB_CONFIG = {
    "host": os.environ.get('DB_WRITER_HOST'),
    "user": os.environ.get('DB_USER', 'admin'),
    "password": os.environ.get('DB_PASSWORD'),
    "database": os.environ.get('DB_NAME', 'raffle_db'),
    "cursorclass": pymysql.cursors.DictCursor
}

def run_draw():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cursor:
            # 1. 추첨 대상 찾기 (종료시간 지남 + 아직 추첨 안 됨)
            cursor.execute("""
                SELECT id, title FROM raffle_items 
                WHERE end_time <= NOW() AND is_drawn = FALSE
            """)
            items_to_draw = cursor.fetchall()

            for item in items_to_draw:
                # 2. 해당 상품 응모자 중 랜덤 1명 추출
                cursor.execute("SELECT user_id FROM raffle_entries WHERE item_id = %s", (item['id'],))
                entries = cursor.fetchall()

                if entries:
                    winner = random.choice(entries)
                    # 3. 당첨자 기록 및 추첨 완료 처리
                    cursor.execute("""
                        UPDATE raffle_items 
                        SET winner_id = %s, is_drawn = TRUE 
                        WHERE id = %s
                    """, (winner['user_id'], item['id']))
                    print(f"[{datetime.now()}] {item['title']} 추첨 완료! 당첨자 ID: {winner['user_id']}")
                else:
                    # 응모자가 없는 경우에도 종료 처리는 해야 함
                    cursor.execute("UPDATE raffle_items SET is_drawn = TRUE WHERE id = %s", (item['id'],))
                    print(f"[{datetime.now()}] {item['title']} 응모자 없음 처리.")
            
        conn.commit()
    finally:
        conn.close()

if __name__ == "__main__":
    run_draw()