const accountRole = document.getElementById('account-role');
const studentNumber = document.getElementById('student-number');
const realName = document.getElementById('real-name');
if (accountRole && studentNumber && realName) {
    const updateIdentity = () => {
        const student = accountRole.value === 'STUDENT';
        studentNumber.closest('.student-field').hidden = !student;
        studentNumber.disabled = !student;
        studentNumber.required = student;
        realName.required = student;
    };
    accountRole.addEventListener('change', updateIdentity);
    updateIdentity();
}
