document.addEventListener('submit', function (event) {
  const form = event.target;
  if (form.method.toLowerCase() !== 'post') return;
  if (form.dataset.sending || (form.dataset.confirm && !window.confirm(form.dataset.confirm))) {
    event.preventDefault();
    return;
  }
  form.dataset.sending = '1';
  setTimeout(() => form.querySelectorAll('button[type="submit"],button:not([type])').forEach(button => { button.disabled = true; }), 0);
});
window.addEventListener('pageshow', function () {
  document.querySelectorAll('form[data-sending]').forEach(form => {
    delete form.dataset.sending;
    form.querySelectorAll('button').forEach(button => { button.disabled = false; });
  });
});
document.addEventListener('click', function (event) {
  const button = event.target.closest('[data-comment]');
  if (!button) return;
  const input = document.getElementById(button.dataset.target);
  if (input) { input.value = button.dataset.comment; input.focus(); }
});
