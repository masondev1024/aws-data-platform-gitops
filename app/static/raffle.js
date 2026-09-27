(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;

  async function responseMessage(response) {
    const body = await response.json().catch(() => ({}));
    return body.message || '요청 처리 중 오류가 발생했습니다.';
  }

  async function applyRaffle(itemId) {
    if (!csrfToken) {
      window.alert('보안 토큰을 확인할 수 없습니다. 페이지를 새로고침해 주세요.');
      return;
    }

    try {
      const response = await fetch('/api/apply', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRFToken': csrfToken,
        },
        body: JSON.stringify({ item_id: itemId }),
      });
      if (response.status === 401) {
        if (window.confirm('로그인 후 응모 가능합니다. 로그인 하시겠습니까?')) {
          window.location.assign('/login');
        }
        return;
      }
      window.alert(await responseMessage(response));
    } catch {
      window.alert('네트워크 오류가 발생했습니다. 잠시 후 다시 시도해주세요.');
    }
  }

  function updateTimers() {
    document.querySelectorAll('.timer').forEach((element) => {
      const endTime = new Date(element.dataset.endtime).getTime();
      const distance = endTime - Date.now();
      if (!Number.isFinite(endTime) || distance < 0) {
        element.textContent = 'CLOSED';
        return;
      }
      const days = Math.floor(distance / 86400000);
      const hours = Math.floor((distance % 86400000) / 3600000);
      const minutes = Math.floor((distance % 3600000) / 60000);
      const seconds = Math.floor((distance % 60000) / 1000);
      element.textContent = `${days}D ${hours}H ${minutes}M ${seconds}S LEFT`;
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.apply-button').forEach((button) => {
      button.addEventListener('click', () => applyRaffle(Number(button.dataset.itemId)));
    });
    updateTimers();
    window.setInterval(updateTimers, 1000);
  });
})();
