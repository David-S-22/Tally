const anomaliesTable = document.getElementById('anomalies');
let sortKey = 'date';
let sortDirection = 1;

function sortAnomalies() {
    const sortValues = {
        date: row => Date.parse(row.dataset.sortDate),
        merchant: row => row.dataset.sortMerchant,
        confidence: row => row.dataset.sortConfidence === '' ? NaN : Number(row.dataset.sortConfidence),
        status: row => ({ unreviewed: 0, confirmed: 1, dismissed: 2 }[row.dataset.sortStatus])
    };
    const getValue = sortValues[sortKey];
    const rows = Array.from(anomaliesTable.querySelectorAll('tr[data-sort-row]'));

    rows.sort((left, right) => {
        const leftValue = getValue(left);
        const rightValue = getValue(right);
        if (typeof leftValue === 'string' && typeof rightValue === 'string') {
            return sortDirection * leftValue.localeCompare(rightValue, undefined, { sensitivity: 'base' });
        }
        const leftInvalid = Number.isNaN(leftValue);
        const rightInvalid = Number.isNaN(rightValue);
        if (leftInvalid || rightInvalid) {
            return leftInvalid === rightInvalid ? 0 : (leftInvalid ? 1 : -1);
        }
        return sortDirection * ((leftValue ?? -Infinity) - (rightValue ?? -Infinity));
    });

    rows.forEach(row => anomaliesTable.appendChild(row));
}

document.addEventListener('click', function (event) {
    const button = event.target.closest('.sort-btn');
    if (!button) return;

    if (sortKey === button.dataset.sortKey) {
        sortDirection *= -1;
    } else {
        sortKey = button.dataset.sortKey;
        sortDirection = 1;
    }
    document.querySelectorAll('.sort-btn').forEach(header => {
        const active = header === button;
        header.closest('th').setAttribute('aria-sort', active
            ? (sortDirection === 1 ? 'ascending' : 'descending')
            : 'none');
    });
    sortAnomalies();
});

document.body.addEventListener('htmx:afterSwap', function (event) {
    if (event.detail.target === anomaliesTable) sortAnomalies();
});

let reviewAnomalyId = null;

function formatTransactionDate(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;

    return new Intl.DateTimeFormat(undefined, {
        weekday: 'long',
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        timeZone: 'UTC'
    }).format(date);
}

function openReviewModal(button) {
    reviewAnomalyId = Number(button.dataset.anomalyId);
    document.getElementById('review-transaction-date').textContent = formatTransactionDate(button.dataset.date);
    document.getElementById('review-transaction-merchant').textContent = button.dataset.merchant;
    document.getElementById('review-transaction-amount').textContent = button.dataset.amount;
    document.getElementById('review-transaction-description').textContent = button.dataset.description;
    document.getElementById('review-transaction-category').textContent = button.dataset.category;
    document.getElementById('review-anomaly-reason').textContent = button.dataset.reason;
    const sourcesList = document.getElementById('review-anomaly-sources');
    const sourcesMissing = document.getElementById('review-anomaly-sources-missing');
    const sources = (button.dataset.sources || '').split('\n').filter(source => source.trim() !== '');
    sourcesList.replaceChildren();
    if (sources.length) {
        sources.forEach(source => {
            const item = document.createElement('li');
            item.className = 'source-chip';
            item.textContent = source;
            item.title = source;
            sourcesList.appendChild(item);
        });
        sourcesList.hidden = false;
        sourcesMissing.hidden = true;
    } else {
        sourcesList.hidden = true;
        sourcesMissing.hidden = false;
    }
    const confidence = document.getElementById('review-confidence');
    const confidenceMissing = document.getElementById('review-confidence-missing');
    const confidenceLevel = button.dataset.confidenceLevel;
    if (confidenceLevel) {
        confidence.className = `confidence-indicator confidence-${confidenceLevel}`;
        confidence.textContent = `${confidenceLevel[0].toUpperCase()}${confidenceLevel.slice(1)} (${button.dataset.confidence}%)`;
        confidence.hidden = false;
        confidenceMissing.hidden = true;
    } else {
        confidence.hidden = true;
        confidenceMissing.hidden = false;
    }
    document.getElementById('review-decision').value = 'confirm';
    const reason = document.getElementById('review-reason');
    reason.value = '';
    reason.required = Boolean(button.dataset.workflowId);
    document.getElementById('review-error').hidden = true;
    const modal = document.getElementById('review-modal');
    modal.classList.remove('closing');
    modal.hidden = false;
}

function closeReviewModal() {
    const modal = document.getElementById('review-modal');
    if (modal.hidden || modal.classList.contains('closing')) return;

    reviewAnomalyId = null;
    modal.classList.add('closing');
}

document.getElementById('review-modal').addEventListener('animationend', function (event) {
    if (event.target === this && this.classList.contains('closing')) {
        this.hidden = true;
        this.classList.remove('closing');
    }
});

document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeReviewModal();
});

async function submitReview() {
    if (reviewAnomalyId === null) return;

    const decision = document.getElementById('review-decision').value;
    const reason = document.getElementById('review-reason').value.trim();
    const reasonField = document.getElementById('review-reason');
    if (reasonField.required && !reason) {
        reasonField.reportValidity();
        return;
    }

    const error = document.getElementById('review-error');
    error.hidden = true;
    try {
        const resp = await fetch('/anomalies-backend/anomalies/' + reviewAnomalyId + '/' + decision, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason })
        });
        if (!resp.ok) {
            const problem = await resp.json().catch(() => null);
            error.textContent = problem?.error || 'The review could not be recorded. Please try again.';
            error.hidden = false;
            return;
        }
        anomaliesTable.innerHTML = await resp.text();
        sortAnomalies();
        closeReviewModal();
    } catch {
        error.textContent = 'The review could not be recorded. Check your connection and try again.';
        error.hidden = false;
    }
}
