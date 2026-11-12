const reportCourse = document.getElementById('report-course');
const reportClass = document.getElementById('report-class');
const reportAssignment = document.getElementById('report-assignment');
if (reportCourse && reportClass && reportAssignment) {
    reportCourse.addEventListener('change', () => {
        reportClass.value = '';
        reportAssignment.value = '';
        for (const option of reportClass.options) {
            option.hidden = Boolean(option.value && reportCourse.value && option.dataset.course !== reportCourse.value);
            option.disabled = option.hidden;
        }
        reportAssignment.disabled = true;
    });
    reportClass.addEventListener('change', () => {
        reportAssignment.value = '';
        reportAssignment.disabled = true;
    });
}
