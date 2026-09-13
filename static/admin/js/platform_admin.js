'use strict';

{
    const navFilter = document.getElementById('nav-filter');
    const advancedPanels = document.querySelectorAll('[data-admin-advanced]');

    // A technical page must remain discoverable when it is the current page.
    advancedPanels.forEach((panel) => {
        if (panel.querySelector('.current-model')) {
            panel.open = true;
        }
    });

    if (navFilter) {
        navFilter.addEventListener('input', () => {
            if (navFilter.value.trim()) {
                advancedPanels.forEach((panel) => {
                    panel.open = true;
                });
            }
        });
    }

    // Pressing "/" focuses navigation search without stealing focus from forms.
    document.addEventListener('keydown', (event) => {
        const target = event.target;
        const isEditing = target instanceof HTMLInputElement
            || target instanceof HTMLTextAreaElement
            || target instanceof HTMLSelectElement
            || target?.isContentEditable;
        if (event.key === '/' && navFilter && !isEditing) {
            event.preventDefault();
            navFilter.focus();
        }
    });
}
