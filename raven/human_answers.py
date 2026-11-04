"""Keep human-authored answers separate from model intent classification."""

ANSWER_FORM_GUIDANCE = '''For kind=answer, also return answer_form: complete, partial, or mixed.
complete means the CURRENT human message is their complete answer or complete replacement answer,
including their own reasons, conditions, exceptions and limits. Code copies that message verbatim;
do not rewrite it, extract a shorter policy, substitute implementation details, or supply a rationale.
Several related requirements, paragraphs, reasons or limits can be ONE complete answer. A prerequisite,
exception, prohibition or statement withholding other authority is part of that answer, not a separate
request. Classify the person's intended actions, not the number of sentences or clauses.
Leave answer and rationale empty for a complete answer. Other action fields must remain empty.
partial means an amendment, fragment or reference that needs the previous answer or conversation
to supply omitted substantive requirements. A complete replacement is complete even when it changes a
proposal. Referring to this task's subject or to the message's own preceding sentences is not by itself
partial. Do not compose or merge a new answer. mixed means the message combines an answer with an
INDEPENDENT request for a referral, new reusable rule, separate action, question or private aside, or contains
quoted/third-party words whose adoption is unclear. Use partial or mixed when completeness is uncertain.
Related conditions and reasons do not themselves make a message mixed. A limit on this answer is not
a request to create a future rule. Keep such words in the authored answer, never in control fields.
Partial and mixed answers require the person to supply a complete replacement before confirmation.
A complete replacement can correct an assumed or unsigned proposal; it is still only a proposal
until the existing human confirmation and complete source-review checks succeed.
Questions, private context, referrals and reusable-rule requests retain their own action kinds.
Never call a routing message or quoted suggestion a complete authored answer.'''

COMPLETE_ANSWER_REQUEST = (
    'I read this as a partial answer or a message with more than one request. '
    'There is no answer ready to confirm. Please send the complete answer or replacement '
    'with `answer: …`, or edit it on your task review page. Send referrals and rule requests separately. '
    'Nothing recorded or signed.'
)

ANSWER_INTERPRETATION_REQUEST = (
    'I could not prepare an answer for confirmation. Please send the complete answer or replacement '
    'with `answer: …`, or edit it on your task review page. Send referrals and rule requests separately. '
    'Nothing recorded or signed.'
)

INLINE_RATIONALE = 'No separate rationale recorded; any reason is included in the complete authored answer.'

_CONTROL_FIELDS = ('reply', 'to', 'conditions', 'expires', 'scope_kind', 'scope', 'contact_outcome', 'required')
_REFUSAL_REASONS = {'partial', 'mixed', 'conflicting_control_fields', 'invalid_message', 'oversized_message'}


class CompleteAnswerRequired(ValueError):
    """The message cannot safely be copied as one complete authored answer."""

    def __init__(self, reason, *, answer_form='', fields=()):
        # Diagnostics contain only bounded enums and known field names. Never
        # preserve model values, its explanation, or the person's private text.
        self.reason = reason if isinstance(reason, str) and reason in _REFUSAL_REASONS else 'unclassified'
        self.answer_form = answer_form if isinstance(answer_form, str) and answer_form in ('complete', 'partial', 'mixed') else ''
        self.fields = tuple(name for name in _CONTROL_FIELDS if isinstance(fields, (list, tuple)) and name in fields)
        super().__init__(COMPLETE_ANSWER_REQUEST if self.reason in ('partial', 'mixed') else ANSWER_INTERPRETATION_REQUEST)

    def diagnostic(self):
        return {'reason': self.reason, 'answer_form': self.answer_form, 'conflicting_fields': list(self.fields)}


def record_refusal(graph, decision, refusal):
    """Record why no reading was offered, without changing decision authority."""
    with graph.transaction():
        graph.append_event('answer_readback_refused', {
            'decision_id': decision['id'], 'task_id': decision['run_id'], **refusal.diagnostic()})


def preserve_complete_answer(action, message):
    """Classification selects a path; it never supplies the answer's words.

    Completeness remains a semantic classification, not a mechanical proof of
    intent. The person still reviews these exact words before anything applies.
    """
    form = action.get('answer_form')
    if form not in ('complete', 'partial', 'mixed'):
        raise ValueError('An answer needs answer_form complete, partial, or mixed')
    if form != 'complete':
        raise CompleteAnswerRequired(form, answer_form=form)
    conflicting = [key for key in _CONTROL_FIELDS if action.get(key)]
    if conflicting:
        raise CompleteAnswerRequired('conflicting_control_fields', answer_form=form, fields=conflicting)
    if not isinstance(message, str) or not message.strip():
        raise CompleteAnswerRequired('invalid_message', answer_form=form)
    if len(message) > 12000:
        raise CompleteAnswerRequired('oversized_message', answer_form=form)
    return {'kind': 'answer', 'answer': message, 'rationale': '', 'original': message,
            'answer_form': 'complete', 'authored_verbatim': True}
