/*
 * reminders-card
 * --------------
 * Dashboard card for the fiendishly_reminders integration.
 *
 * Why this exists rather than the built-in to-do card pointed at todo.reminders:
 * a TodoItem has fields for summary, status, due and description and NOTHING for
 * recurrence, so the to-do card cannot say that a reminder repeats, let alone how
 * often. It shows "take the trash out, Tuesday 7:15" and gives no hint that there
 * is another one every Tuesday after that. It also cannot express the one action a
 * repeating reminder actually needs - skip THIS occurrence and leave the series
 * alone - because ticking an item is the only verb it has.
 *
 * The data therefore comes from sensor.reminders_next_reminder rather than from the
 * to-do entity. The store keeps ONE row per reminder at its next occurrence and the
 * sensor publishes that list in an attribute, already carrying `every` as prose
 * ("every Tuesday", "on the 1st of every month") and a `recurring` flag. Nothing
 * here re-derives an RRULE.
 *
 * That also means no polling: the attribute arrives with every state update, so the
 * card re-renders when the data changes and not on a timer.
 *
 * Actions call the integration's services by `id`, never by text. Matching a
 * reminder by its text is for speech, where a name is all you have; a card knows
 * exactly which row was pressed and should not throw that away and describe it back
 * as a string that might match two things.
 *
 * Canceling asks first, inline, because for a repeating reminder it removes every
 * future occurrence - the same reason the voice path reads the match back before
 * doing anything. Skipping does not ask: it drops one occurrence and says which.
 *
 * Adding a reminder goes through conversation/process against the LOCAL agent
 * (conversation.home_assistant), not through the schedule service. Typing "take the
 * trash out every Tuesday at 7 am" then hits the very same grammar and intent handlers
 * as saying it out loud, so recurrence, relative times and "tomorrow morning" all work
 * without the card reimplementing any of it - and a phrase the grammar does not know
 * gets the same honest refusal rather than a silently wrong reminder. Pinning the agent
 * matters: the default pipeline agent is the LLM, which would answer warmly and schedule
 * nothing. Nothing is echoed back on success - the new row is the confirmation - but a
 * refusal is shown, because that produces no row and silence would look the same as a
 * reminder that vanished.
 *
 * Palette is driven from config (accent_color / text_color / background / border /
 * border_radius / font_family) rather than from card_mod, which cannot style this
 * card: the render rewrites innerHTML, so injected styles are discarded on every
 * update. Unset, the keys fall back to the theme.
 */

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                     'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

class RemindersCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      entity: 'sensor.reminders_next_reminder',
      header: 'Reminders',
      max: 12,
      show_empty: true,
      accent_color: '',
      text_color: '',
      skip_color: '#ffe600',
      cancel_color: '#ff1a1a',
      day_color: '#a8c4ff',
      yes_color: '#ff4d4d',
      no_color: '#4cd964',
      action_gap: '22px',
      allow_add: true,
      add_placeholder: 'Enter new reminder....',
      background: '',
      border: '',
      border_radius: '',
      font_family: '',
      ...config,
    };
    this._skin = {
      accent: this._config.accent_color || 'var(--primary-text-color)',
      text: this._config.text_color || this._config.accent_color || 'var(--primary-text-color)',
      bg: this._config.background || 'var(--ha-card-background, var(--card-background-color))',
      border: this._config.border || '',
      radius: this._config.border_radius || '',
      font: this._config.font_family || 'inherit',
      skip: this._config.skip_color,
      cancel: this._config.cancel_color,
      day: this._config.day_color,
      yes: this._config.yes_color,
      no: this._config.no_color,
      gap: this._config.action_gap,
    };
    this._adding = false;
    this._reply = '';
    this._confirming = null;   // {id, act} of the row currently asking a question
    this._sig = null;
    this.innerHTML = '';
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    const n = this._rows() ? this._rows().length : 3;
    return Math.min(12, 2 + n);
  }

  static getStubConfig() {
    return { type: 'custom:reminders-card', entity: 'sensor.reminders_next_reminder' };
  }

  _rows() {
    const st = this._hass && this._hass.states[this._config.entity];
    if (!st) return null;
    return st.attributes.reminders || [];
  }

  /* Day heading for a reminder: Today / Tomorrow / a weekday within the week / a date. */
  _dayLabel(due, now) {
    const d = new Date(due.getFullYear(), due.getMonth(), due.getDate());
    const t = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const days = Math.round((d - t) / 86400000);
    if (days === 0) return 'Today';
    if (days === 1) return 'Tomorrow';
    if (days > 1 && days < 7) return DAY_NAMES[due.getDay()];
    return `${MONTH_NAMES[due.getMonth()]} ${due.getDate()}`;
  }

  _clock(due) {
    const h = due.getHours() % 12 || 12;
    const m = String(due.getMinutes()).padStart(2, '0');
    return `${h}:${m} ${due.getHours() < 12 ? 'AM' : 'PM'}`;
  }

  /* Display only - the stored text is left exactly as spoken, because that is what the
     voice matching searches. Capitalising it here would not change what "cancel my walk
     Ozzy reminder" has to match against. */
  _cap(t) {
    const s = String(t == null ? '' : t);
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  }

  _call(service, data) {
    // Optimistic nothing: the sensor pushes a new attribute list as soon as the store
    // changes, so the row disappears on its own. Re-rendering here would race that.
    this._confirming = null;
    // Name the list. With one person set up this is redundant; with two there is no
    // default any more, and a call that names nobody is refused rather than guessed at.
    const whose = this._hass.states[this._config.entity]?.attributes?.list;
    if (whose) data = { ...data, target: whose };
    this._hass.callService('fiendishly_reminders', service, data).catch((e) => {
      console.error('reminders-card:', service, 'failed', e);
    });
  }

  _render() {
    const rows = this._rows();
    if (rows === null) {
      this._paint(`<div class="rc-msg">No entity <code>${this._esc(this._config.entity)}</code>.
        Is the Reminders integration set up?</div>`);
      return;
    }

    // Re-render only when something actually changed. `set hass` fires on every state
    // update in the house, and rebuilding innerHTML each time would drop the pressed
    // state of a confirmation mid-tap.
    const sig = JSON.stringify(rows.map((r) => [r.id, r.due, r.text, r.every, r.is_last])) +
                '|' + JSON.stringify(this._confirming) + '|' + this._adding + '|' + this._reply;
    if (sig === this._sig) return;
    this._sig = sig;

    if (!rows.length) {
      if (!this._config.show_empty) { this._paint(''); return; }
      this._paint(`<div class="rc-empty">
        <ha-icon icon="mdi:bell-sleep-outline"></ha-icon>
        <span>Nothing scheduled.</span></div>${this._addBox()}`);
      return;
    }

    const now = new Date();
    const shown = rows.slice(0, Number(this._config.max) || 12);
    let html = '';
    let lastDay = null;

    shown.forEach((r) => {
      const due = new Date(r.due);
      const day = this._dayLabel(due, now);
      if (day !== lastDay) {
        lastDay = day;
        html += `<div class="rc-day">${this._esc(day)}</div>`;
      }
      const asking = this._confirming && this._confirming.id === r.id
        ? this._confirming.act : null;
      const meta = r.recurring
        ? `<span class="rc-every"><ha-icon icon="mdi:repeat"></ha-icon>${this._esc(r.every)}</span>`
        : '';
      html += `
        <div class="rc-row${asking ? ' rc-asking' : ''}">
          <div class="rc-time">${this._esc(this._clock(due))}</div>
          <div class="rc-body">
            <div class="rc-text">${this._esc(this._cap(r.text))}</div>
            ${meta}
          </div>
          <div class="rc-actions">
            ${asking ? `
              <span class="rc-ask">${asking === 'skip'
                ? 'Last one. Remove it?'
                : `Cancel${r.recurring ? ' all of them' : ''}?`}</span>
              <button class="rc-btn rc-yes" data-act="${asking === 'skip' ? 'skip-go' : 'cancel'}"
                  data-id="${this._esc(r.id)}">Yes</button>
              <button class="rc-btn rc-no" data-act="abort" data-id="${this._esc(r.id)}">No</button>
            ` : `
              ${r.recurring ? `<button class="rc-icon rc-skip"
                  title="${r.is_last ? 'Skip the last one - this removes the reminder' : 'Skip this one'}"
                  data-act="${r.is_last ? 'ask-skip' : 'skip'}" data-id="${this._esc(r.id)}">
                  <ha-icon icon="mdi:debug-step-over"></ha-icon></button>`
                : `<span class="rc-icon-gap"></span>`}
              <button class="rc-icon rc-cancel" title="Cancel"
                  data-act="ask" data-id="${this._esc(r.id)}">
                  <ha-icon icon="mdi:close"></ha-icon></button>
            `}
          </div>
        </div>`;
    });

    if (rows.length > shown.length) {
      html += `<div class="rc-more">+${rows.length - shown.length} more</div>`;
    }
    html += this._addBox();
    this._paint(html, rows.length);
  }

  /* Deliberately its own block under the list rather than a last row in it: it is not a
     reminder, and sitting in the run of rows made it read as one. */
  _addBox() {
    if (!this._config.allow_add) return '';
    if (!this._adding) {
      return `<div class="rc-addwrap">
        <button class="rc-add" data-act="open-add">
          <ha-icon icon="mdi:plus"></ha-icon><span>Add a reminder</span></button>
        ${this._reply ? `<div class="rc-reply">${this._esc(this._reply)}</div>` : ''}
      </div>`;
    }
    return `<div class="rc-addwrap rc-adding">
      <input class="rc-input" type="text" autocomplete="off"
        placeholder="${this._esc(this._config.add_placeholder)}">
      <div class="rc-addbtns">
        <button class="rc-btn rc-go" data-act="submit-add">Add</button>
        <button class="rc-btn" data-act="close-add">Cancel</button>
      </div>
      ${this._reply ? `<div class="rc-reply">${this._esc(this._reply)}</div>` : ''}
    </div>`;
  }

  /* Send the typed phrase to the LOCAL conversation agent, so it takes the same path a
     spoken reminder does. "remind me to" is prepended only when the user has not written
     it, which lets both "walk the dog at 5 pm" and a full sentence work. */
  async _submitAdd() {
    const input = this.querySelector('.rc-input');
    const text = (input && input.value || '').trim();
    if (!text) return;
    const phrase = /^\s*remind me\b/i.test(text) ? text : `remind me to ${text}`;
    this._reply = '';
    try {
      const res = await this._hass.callWS({
        type: 'conversation/process',
        text: phrase,
        agent_id: 'conversation.home_assistant',
      });
      const speech = (res && res.response && res.response.speech
        && res.response.speech.plain && res.response.speech.plain.speech) || '';
      // Nothing is said back on success: the new row appearing in the list above IS the
      // confirmation, and repeating it in words is noise. A FAILURE still has to speak,
      // because it produces no row - a phrase the grammar does not know would otherwise
      // look identical to a reminder that was silently dropped.
      //
      // "Alright" is how every one of the scheduling intents opens its success reply and
      // no failure branch does, so it is the success signal. That couples this card to
      // the wording in custom_components/fiendishly_reminders/intents.py; the coupling
      // fails SAFE, since an unrecognized reply keeps the box open and shows why.
      if (/^Alright/i.test(speech)) {
        this._adding = false;
        if (input) input.value = '';
      } else {
        this._reply = speech || 'Nothing was added.';
      }
    } catch (e) {
      this._reply = `Could not add: ${e.message || e}`;
    }
    this._sig = null;
    this._render();
  }

  _paint(inner, count) {
    const s = this._skin;
    const head = this._config.header
      ? `<div class="rc-head"><ha-icon icon="mdi:bell-ring-outline"></ha-icon>
         <span>${this._esc(this._config.header)}</span>
         ${count ? `<span class="rc-count">${count}</span>` : ''}</div>`
      : '';
    this.innerHTML = `
      <ha-card style="
        background:${s.bg};
        ${s.border ? `border:${s.border};` : ''}
        ${s.radius ? `border-radius:${s.radius};` : ''}
        font-family:${s.font};
        color:${s.text};
        overflow:hidden;">
        <style>
          .rc-head{display:flex;align-items:center;gap:8px;padding:12px 14px 6px;
            font-weight:700;letter-spacing:.02em;color:${s.accent};}
          .rc-head ha-icon{--mdc-icon-size:20px;}
          .rc-count{margin-left:auto;font-size:.8em;opacity:.75;
            border:1px solid ${s.accent};border-radius:999px;padding:0 8px;}
          .rc-day{padding:10px 14px 3px;font-size:.72em;text-transform:uppercase;
            letter-spacing:.09em;color:${s.day};text-align:center;}
          .rc-row{display:flex;align-items:center;gap:10px;padding:7px 14px;}
          .rc-row+.rc-row{border-top:1px solid ${s.accent}22;}
          .rc-asking{background:${s.accent}14;}
          .rc-time{flex:0 0 78px;font-variant-numeric:tabular-nums;font-size:.9em;opacity:.9;}
          .rc-body{flex:1 1 auto;min-width:0;}
          .rc-text{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
          .rc-every{display:inline-flex;align-items:center;gap:4px;font-size:.75em;
            opacity:.7;margin-top:2px;}
          .rc-every ha-icon{--mdc-icon-size:13px;}
          .rc-actions{flex:0 0 auto;display:flex;align-items:center;gap:${s.gap};}
          .rc-icon{background:none;border:0;cursor:pointer;opacity:.85;
            padding:2px;line-height:0;border-radius:6px;}
          .rc-icon:hover{opacity:1;}
          /* A thin black halo, doubled up because one drop-shadow at 1px is barely
             there. text-stroke does not apply to the icon's SVG. */
          .rc-icon ha-icon{--mdc-icon-size:21px;
            filter:drop-shadow(0 0 1px #000) drop-shadow(0 0 1px #000)
                   drop-shadow(0 0 .5px #000);}
          /* A one-off has no skip button; this keeps its cancel in the same column as
             a repeating reminder's, so the icons do not jump about between rows. */
          .rc-icon-gap{display:inline-block;width:25px;}
          .rc-skip{color:${s.skip};}
          .rc-skip:hover{background:${s.skip}26;}
          .rc-cancel{color:${s.cancel};}
          .rc-cancel:hover{background:${s.cancel}26;}
          .rc-ask{font-size:.78em;opacity:.85;}
          .rc-btn{background:none;cursor:pointer;color:inherit;font:inherit;font-size:.78em;
            border:1px solid ${s.accent}66;border-radius:6px;padding:1px 9px;}
          .rc-btn:hover{background:${s.accent}22;}
          .rc-yes{color:${s.yes};border-color:${s.yes};font-weight:700;}
          .rc-yes:hover{background:${s.yes}26;}
          .rc-no{color:${s.no};border-color:${s.no};font-weight:700;}
          .rc-no:hover{background:${s.no}26;}
          .rc-go{color:${s.no};border-color:${s.no};font-weight:700;}
          .rc-addwrap{display:flex;flex-direction:column;align-items:center;gap:10px;
            padding:16px 14px 18px;margin-top:8px;border-top:1px solid ${s.accent}22;}
          .rc-add{display:inline-flex;align-items:center;justify-content:center;gap:10px;
            background:none;border:2px dashed ${s.accent}66;border-radius:12px;
            color:inherit;font:inherit;font-size:1.6em;font-weight:600;
            padding:10px 22px;cursor:pointer;opacity:.85;}
          .rc-add:hover{opacity:1;background:${s.accent}18;}
          .rc-add ha-icon{--mdc-icon-size:32px;}
          .rc-input{width:100%;max-width:340px;box-sizing:border-box;
            background:rgba(0,0,0,.35);color:inherit;font:inherit;font-size:.95em;
            text-align:center;border:1px solid ${s.accent}55;border-radius:8px;
            padding:8px 10px;}
          /* Full opacity in the day-heading blue: the placeholder is the only hint of
             the phrasing this box expects, so it has to be readable, not decorative. */
          .rc-input::placeholder{color:${s.day};opacity:1;}
          .rc-input:focus{outline:none;border-color:${s.accent};}
          .rc-addbtns{display:flex;gap:12px;}
          .rc-addbtns .rc-btn{font-size:.95em;padding:4px 16px;}
          .rc-reply{font-size:.82em;opacity:.85;font-style:italic;text-align:center;}
          .rc-empty{display:flex;align-items:center;gap:8px;padding:16px 14px;opacity:.6;}
          .rc-more,.rc-msg{padding:8px 14px 12px;font-size:.8em;opacity:.6;}
        </style>
        ${head}${inner}
      </ha-card>`;

    this.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', (ev) => {
        ev.stopPropagation();
        const { act, id } = el.dataset;
        if (act === 'ask')      { this._confirming = { id, act: 'cancel' }; this._sig = null; this._render(); return; }
        // Skipping the LAST occurrence exhausts the rule and deletes the reminder, so it
        // asks like a cancellation does - which is what it is.
        if (act === 'ask-skip') { this._confirming = { id, act: 'skip' };   this._sig = null; this._render(); return; }
        if (act === 'abort')    { this._confirming = null; this._sig = null; this._render(); return; }
        if (act === 'skip' || act === 'skip-go') { this._call('skip', { id, scope: 'next' }); return; }
        if (act === 'cancel')   { this._call('cancel', { id }); return; }
        if (act === 'open-add')  { this._adding = true;  this._reply = ''; this._sig = null; this._render(); return; }
        if (act === 'close-add') { this._adding = false; this._reply = ''; this._sig = null; this._render(); return; }
        if (act === 'submit-add'){ this._submitAdd(); }
      });
    });

    const input = this.querySelector('.rc-input');
    if (input) {
      input.focus();
      input.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); this._submitAdd(); }
        if (ev.key === 'Escape') {
          this._adding = false; this._reply = ''; this._sig = null; this._render();
        }
      });
      // Bubble closes a pop-up on a swipe; a drag inside a text field is not one.
      ['touchstart', 'touchmove', 'click'].forEach((t) =>
        input.addEventListener(t, (ev) => ev.stopPropagation()));
    }
  }
}

customElements.define('reminders-card', RemindersCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'reminders-card',
  name: 'Reminders',
  description: 'Upcoming reminders with recurrence, skip and cancel.',
});
