(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

  async function signup() {
    const username = document.getElementById('uid').value;
    const password = document.getElementById('pw').value;
    if (!username || !password) {
      window.alert('아이디와 비밀번호를 모두 입력해주세요.');
      return;
    }
    if (!csrfToken) {
      window.alert('보안 토큰을 확인할 수 없습니다. 페이지를 새로고침해 주세요.');
      return;
    }

    try {
      const response = await fetch('/api/signup', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify({ username, password }),
      });
      const body = await response.json().catch(() => ({}));
      window.alert(body.message || '회원가입 요청을 처리할 수 없습니다.');
      if (response.ok) {
        window.location.assign('/login');
      }
    } catch {
      window.alert('네트워크 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.getElementById('signup-button').addEventListener('click', signup);
  });
})();
