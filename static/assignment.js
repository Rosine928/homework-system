const courseSelect = document.getElementById('create-course');
const classSelect = document.getElementById('create-class');
if (courseSelect && classSelect) {
    const updateClasses = () => {
        for (const option of classSelect.options) {
            option.hidden = !!option.value && option.dataset.course !== courseSelect.value;
            option.disabled = option.hidden;
        }
        if (classSelect.selectedOptions[0]?.disabled) classSelect.value = '';
    };
    courseSelect.addEventListener('change', updateClasses);
    updateClasses();
}
