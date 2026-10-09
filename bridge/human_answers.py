"""Keep human-authored answers separate from model intent classification."""

ANSWER_FORM_GUIDANCE = '''For kind=answer, also return answer_form: complete, partial, or mixed.
complete means the CURRENT human message is their complete answer or complete replacement answer,
including their own reasons, conditions, exceptions and limits. Code copies that message verbatim;
do not rewrite it, extract a shorter policy, substitute implementation details, or supply a rationale.
Leave answer and rationale empty for a complete answer. Other action fields must remain empty.
partial means an amendment, fragment or reference that needs the previous answer or conversation
to become a complete replacement. Do not compose or merge a new answer. mixed means the message
combines an answer with a referral, new rule, separate action, question or private aside, or contains
quoted/third-party words whose adoption is unclear. Use partial or mixed when completeness is uncertain.
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

INLINE_RATIONALE = 'No separate rationale recorded; any reason is included in the complete authored answer.'


class CompleteAnswerRequired(ValueError):
    """The message cannot safely be copied as one complete authored answer."""


def preserve_complete_answer(action, message):
    """Classification selects a path; it never supplies the answer's words.

    Completeness remains a semantic classification, not a mechanical proof of
    intent. The person still reviews these exact words before anything applies.
    """
    form = action.get('answer_form')
    if form not in ('complete', 'partial', 'mixed'):
        raise ValueError('An answer needs answer_form complete, partial, or mixed')
    if form != 'complete' or any(action.get(key) for key in (
            'reply', 'to', 'conditions', 'expires', 'scope_kind', 'scope', 'contact_outcome', 'required')):
        raise CompleteAnswerRequired(COMPLETE_ANSWER_REQUEST)
    if not isinstance(message, str) or not message.strip() or len(message) > 12000:
        raise CompleteAnswerRequired(COMPLETE_ANSWER_REQUEST)
    return {'kind': 'answer', 'answer': message, 'rationale': '', 'original': message,
            'answer_form': 'complete', 'authored_verbatim': True}
