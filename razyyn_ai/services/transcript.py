# Copyright (c) 2026, Marwan Badr and contributors
# For license information, please see LICENSE

# GENERATED FILE -- DO NOT EDIT.
#
# Copied verbatim out of the Frappe app by tools/sync_from_frappe.py, from the
# blocks marked "SHARED WITH THE ODOO MODULE" in
#   accountant_agent/accountant_agent/page/agent_chat/agent_chat.py
#
# WHY IT IS SHARED RATHER THAN REWRITTEN HERE
#     What these functions do is not plumbing -- it is the difference between a
#     customer reading their answer and a customer reading
#     `{"type": "clarification", "questions": [{"id": ...`. Each rule in here
#     was put there by a specific complaint about a specific screen, and a
#     second implementation would have to be told about each of those
#     complaints again, one regression at a time.
#
# To change any of it: edit the Frappe app, then run
#   python3 tools/sync_from_frappe.py
# `--check` fails the build when this file and the Frappe source disagree.

import json
import re
from base64 import b64decode, b64encode
from html import escape, unescape

from odoo import _



#: What the chat page hides in a stored message so its own widgets survive a
#: reload: the base64 question payload, and the fold around it.
_CARRIED_MARKUP = re.compile(
	r'<span[^>]*class="agent-question-data"[^>]*>\s*</span>|</?(?:details|summary)[^>]*>',
	re.IGNORECASE,
)

#: The base64 question payload, wherever it rides — on the fold or on the
#: legacy span. Both shapes carry the identical JSON.
_QUESTION_PAYLOAD = re.compile(r'data-questions="([A-Za-z0-9+/=]+)"')

#: What the customer answered, out of the block that asked it.
#:
#: MATCHED ON A CLASS, NEVER ON THE LABEL. The label is translated — a site
#: running in Arabic writes "أجبت:" — so a regex for the English words finds
#: nothing there and the answer is silently dropped from both the model's
#: history and the "has this been answered?" test. The class is the same
#: string in every language.
_ANSWERED = re.compile(
	r'<span[^>]*class="agent-answer"[^>]*>(.*?)</span>',
	re.IGNORECASE | re.DOTALL,
)

#: The wrapper that carries it. What this marks is the whole line, so the
#: customer reads it and the renderer can still find it.
_ANSWER_BLOCK = '<span class="agent-answer">{label}</span>'

#: On the fold itself, so the browser can tell an open question from a
#: settled one WITHOUT parsing the body. The answer picker reopens only for
#: a question still waiting; without this it would reopen on an exchange
#: that finished ten minutes ago, because the block is still the last
#: message in the session.
_ANSWERED_FLAG = ' data-answered="1"'


def _questions_only(content: str) -> str:
	"""The questions out of a stored question turn, and nothing else.

	Returns "" when this turn is not a question, so the caller falls through to
	the ordinary prose path.

	WHY THIS READS THE PAYLOAD RATHER THAN THE PROSE
		The stored turn is prose written for a person, and prose accretes. A
		question the agent had to ask twice carries an apology in front of it —
		*Sorry, I could not match "don't recodr , they are draft, just submit
		them" to a record in your system* — and behind it the standing guidance
		*tell me the name as it appears in your books*.

		Stripping the tags left every one of those lines in the transcript that
		is handed to the model. Four turns of it and the conversation reads as
		an agent whose job is to ask for names, so the model asks for a name:
		the live log shows the same question put three times to a customer who
		had answered it in their first message. *"next nodes can not know whawt
		the user say here"* is exactly that, and it is a signal-to-noise
		problem rather than a plumbing one.

		The payload is STRUCTURED. It holds the question text and nothing that
		was wrapped around it, so reading it cannot pick up prose that has not
		been written yet.

	THE OPTIONS ARE DROPPED HERE TOO, and they are dropped on purpose. The
	choice has been made by the time anybody reads this back; the roads not
	taken sit between the question and the answer making the exchange harder to
	read, and they spend a character budget that the turn carrying the amount
	needs.
	"""
	match = _QUESTION_PAYLOAD.search(content or "")
	if not match:
		return ""
	try:
		questions = json.loads(b64decode(match.group(1)).decode("utf-8"))
	except Exception:
		return ""
	if not isinstance(questions, list):
		return ""
	asked = [
		str(q.get("question") or "").strip()
		for q in questions
		if isinstance(q, dict) and str(q.get("question") or "").strip()
	]
	if not asked:
		return ""

	# AND THE ANSWER, WHICH NOW LIVES IN THE SAME ROW.
	#
	# This is the one place the fold can silently break the thing it was built
	# for. Reading only the payload would hand the model the question and drop
	# the reply — *"these questions go with chat history to llm so it can
	# understand the context and never ask user the same questions again"* is
	# the reply, not the question.
	#
	# Taken from the rendered body rather than from the payload, because the
	# payload is what was ASKED and the answer was never in it.
	said = _ANSWERED.search(content or "")
	if said:
		asked.append(unescape(said.group(1)).strip())
	return "\n".join(asked)


def _prose_only(content: str) -> str:
	"""One stored turn, as the sentence the person actually read.

	THREE THINGS LIVE IN THIS COLUMN AND ONLY ONE OF THEM IS PROSE.
		A plain reply is prose already. A pause is stored as the JSON envelope
		the client needs in order to draw its card. A question carries a
		base64 payload of its questions in an invisible span, or on a fold.

	The agent is sent the transcript so it can remember what was agreed —
	*"it should send to llm in chat history, so it can get the context and
	avoide ask the user the same question twice"*. Handing it a JSON envelope
	teaches it to answer in JSON, and handing it several hundred characters of
	base64 spends a per-turn character budget on nothing at all: the turn that
	gets truncated to make room is the one where the customer said what the
	entry was for.

	A QUESTION TURN IS REDUCED TO THE QUESTION. See `_questions_only`.
	"""
	text = (content or "").strip()
	if not text:
		return ""

	asked = _questions_only(text)
	if asked:
		return asked

	if text.startswith("{"):
		try:
			payload = json.loads(text)
		except (ValueError, TypeError):
			payload = None
		if isinstance(payload, dict):
			spoken = payload.get("plan") or payload.get("question") or payload.get("response") or ""
			text = str(spoken).strip() or text
	# AFTER THE JSON, NEVER BEFORE IT. The chat page stores what a person typed
	# HTML-escaped, so `don't` is on disk as `don&#x27;t` and reached the model
	# looking like markup. Unescaping earlier would be a different bug: a
	# `&quot;` inside a stored envelope becomes a real quote and closes the JSON
	# string early, so the parse above fails and the customer's turn is handed
	# over raw.
	return unescape(_CARRIED_MARKUP.sub("", text).strip())


def _readable_response(ai_response: str) -> tuple:
	"""Split an agent reply into what a person reads and what a picker needs.

	Returns (the prose to show and store, the questions to open a picker for).

	WHY THIS EXISTS
		An agent that pauses answers with a JSON envelope — the client needs
		the questions and their options as structured data, and there is no
		way around that. But the envelope was going straight into the chat
		transcript as the assistant's message, so the customer saw:

			{"type": "clarification", "questions": [{"id": "essentials", ...

		...with the question text buried inside it, twice. `project_rules.md`
		§6 forbids exactly this, and it was landing on the one screen the
		product is judged by.

		Every envelope already carries its own rendered prose, written by the
		agent for this purpose. This returns that, and hands the structured
		half to the picker instead of to the renderer.

	ANYTHING UNRECOGNISED IS RETURNED UNTOUCHED. A normal reply is not
	JSON, and a future envelope shape this does not know about must still
	reach the customer rather than being swallowed by a parser.
	"""
	if not ai_response or not ai_response.lstrip().startswith("{"):
		return ai_response, []
	try:
		payload = json.loads(ai_response)
	except (ValueError, TypeError):
		return ai_response, []
	if not isinstance(payload, dict):
		return ai_response, []

	if payload.get("type") != "clarification":
		# A PLAN ENVELOPE IS STORED VERBATIM, AND MUST BE.
		#
		# `get_latest_plan_message` finds a pending approval by looking for a
		# stored message whose content literally starts with `{"type": "plan"`,
		# and `handle_agent_message` then rewrites its `status` field from
		# "pending" to "approved" or "refused". Replacing that message with its
		# own prose makes the plan unfindable, so the status never moves — and
		# the failure is silent, because the caller swallows the parse error.
		#
		# The chat window renders a plan as a card from the same JSON. Only the
		# clarification envelope was ever shown to a customer raw, and only it
		# is rewritten here.
		return ai_response, []

	questions = payload.get("questions")
	spoken = payload.get("question") or ""
	return (spoken or ai_response), (questions if isinstance(questions, list) else [])


def _collapsible_question(spoken: str, questions: list, answer: str = "") -> str:
	"""The question as it belongs in a transcript, with the answer inside it.

	THE REQUEST, IN FIVE PARTS, AND IT HAS BEEN THE SAME REQUEST EVERY TIME
		*"user qustions should stored in history wiht user reply"*, then
		*"questions to the user should not saved to the chat with its options,
		just save the question and the answer that the user choosed"*, then
		*"it should be collabsable so user can oben or close to save chat window
		space in ui"*, then *"i said before the question should saved with just
		user answer, but you saved the question with all option"*, and now:
		*"i siad save qestions and its answer in chat history and it should
		appear in the chat so user can see lasy question and his reply, and it
		should be collabsable so user can open and close"*.

	SO IT IS ONE BLOCK, AND BOTH HALVES ARE IN IT
		The summary is the question, closed by default — one line of chat window
		per exchange. Open it and there is what they answered. A conversation
		that recorded eight documents is eight lines, and every one of them can
		be opened to see what was decided.

		The previous attempt made a lone question plain text, on the reasoning
		that a fold with nothing in it is a control that does nothing. That was
		true of a fold with nothing in it, and the answer is not to remove the
		fold — it is to put the answer inside it, which is what was asked for
		three times before.

	WRITTEN TWICE: ONCE WITHOUT THE ANSWER, ONCE WITH IT
		The question is stored the moment the agent pauses, because the customer
		is looking at it then and a reload must not lose it. When they answer,
		`fold_the_answer_in` rewrites that same row through here with `answer`
		filled in. Nothing is stored twice and nothing is lost if the rewrite
		never happens — an unanswered question is a fold with the question in it.

	THE OPTIONS ARE NOT STORED AND NEVER WERE. The choice has been made; the
	answer is the next line inside the same block, and the roads not taken are
	dead weight in a transcript read by a model with a character budget.

	THE PICKER MUST STILL REOPEN AFTER A RELOAD. The transcript renderer finds
	the questions by their `data-questions` attribute, so the structured
	questions ride along on the block. Base64 rather than escaped JSON on
	purpose: the payload then contains no character that HTML, Markdown or the
	no-Markdown fallback can react to, and Arabic question text survives it.
	"""
	if not spoken or not questions:
		return spoken

	asked = [
		str(question.get("question") or "").strip()
		for question in questions
		if isinstance(question, dict) and str(question.get("question") or "").strip()
	]
	if not asked:
		return spoken

	packed = b64encode(json.dumps(questions, ensure_ascii=False).encode("utf-8")).decode("ascii")

	headline = asked[0]
	if len(asked) > 1:
		headline = _("{0} (and {1} more)").format(headline, len(asked) - 1)

	# `question` is LLM-authored text (asked[1:] comes straight from the
	# clarification payload), so it gets the same escape() treatment as
	# `headline` (asked[0]) and the answer below — unescaped it is a stored
	# XSS: this string is persisted verbatim into Agent Chat History.content
	# and later rendered back through parse_markdown -> .html().
	body = "\n".join(f"{index}. {escape(question)}" for index, question in enumerate(asked[1:], 2))

	said = (answer or "").strip()
	if said:
		# THEIR OWN WORDS, LABELLED AND MARKED. The label is for the customer
		# and is translated; the class is for the renderer and never is.
		body = (body + "\n\n" if body else "") + _ANSWER_BLOCK.format(
			label=escape(_("You answered: {0}").format(said)),
		)

	else:
		body = (body + "\n\n" if body else "") + _("Awaiting your answer")

	# The blank line after </summary> is load-bearing: without it a Markdown
	# renderer treats the body as raw HTML and the content comes out as one
	# unformatted run-on line.
	settled = _ANSWERED_FLAG if said else ""
	return (
		f'<details class="agent-question" data-questions="{packed}"{settled}>\n'
		f"<summary>{escape(headline)}</summary>\n\n"
		f"{body}\n"
		"</details>"
	)


#: THE CARRIER FOR A QUESTION THAT IS NOT FOLDED. Invisible in the transcript,
#: and the only thing that lets the answer picker reopen after a reload. It
#: predates the fold and was very nearly deleted with it; a lone question has
#: nothing to fold, so it is the right shape again rather than a legacy one.
_QUESTION_DATA = '<span class="agent-question-data" data-questions="{packed}"></span>'


def _answer_text(message: str) -> str:
	"""What the customer actually answered, out of the envelope around it.

	A reply to a question arrives as the agent's own question with the answer
	appended to it:

		Clarification Response:
		* **Got it, we can record the laptop sale as revenue. Two ways ...**: sale invoice

	That whole blob was never stored, so the transcript showed a question and
	then nothing — the customer's own words disappeared from their chat, and
	so did the fact that they had answered at all. Storing the blob verbatim
	would be worse: it repeats the question back at them at full length.

	Returns "" when there is no answer to show, and the caller stores nothing.
	"""
	answers = []
	for line in (message or "").splitlines():
		line = line.strip()
		if not line.startswith("*"):
			continue
		# "* **<question>**: <answer>" — the answer is after the last "**:".
		marker = "**:"
		if marker in line:
			said = line.rsplit(marker, 1)[1].strip()
		else:
			said = line.lstrip("*").strip()
		if said:
			answers.append(said)

	if not answers:
		return ""
	return "\n".join(answers)
