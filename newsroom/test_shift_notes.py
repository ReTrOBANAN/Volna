"""Card-triggered note tooltips keep plain text safe and hidden until requested."""
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import BroadcastShift, Department
from .permissions import assign_role, setup_roles

User = get_user_model()


class ParsedNotes(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        index = len(self.nodes)
        self.nodes.append({'tag': tag, 'attrs': dict(attrs), 'parent': self.stack[-1] if self.stack else None, 'text': ''})
        if tag not in self.VOID:
            self.stack.append(index)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for offset in range(len(self.stack) - 1, -1, -1):
            if self.nodes[self.stack[offset]]['tag'] == tag:
                del self.stack[offset:]
                break

    def handle_data(self, data):
        for index in self.stack:
            self.nodes[index]['text'] += data

    def within(self, child, ancestor):
        parent = self.nodes[child]['parent']
        while parent is not None:
            if parent == ancestor:
                return True
            parent = self.nodes[parent]['parent']
        return False

    def marked(self, attribute):
        return [(index, node) for index, node in enumerate(self.nodes) if attribute in node['attrs']]


@override_settings(TIME_ZONE='Asia/Novosibirsk')
class ShiftNotePopupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        setup_roles()
        department = Department.objects.get(slug='radio')
        cls.chief = User.objects.create_user(username='note_popup_chief')
        assign_role(cls.chief, 'radio_chief', department)
        cls.reporter = User.objects.create_user(username='note_popup_reporter', first_name='Радиокорреспондент')
        assign_role(cls.reporter, 'radio_reporter', department)
        cls.day = date(2027, 6, 15)

    def setUp(self):
        self.client.force_login(self.chief)
        self.calendar_url = reverse('newsroom:shifts')

    def shift(self, notes='', hour=10):
        start = datetime.combine(self.day, time(hour), tzinfo=ZoneInfo('Asia/Novosibirsk'))
        return BroadcastShift.objects.create(employee=self.reporter, created_by=self.chief,
                                             starts_at=start, ends_at=start + timedelta(hours=1), notes=notes)

    def parsed(self, response):
        parsed = ParsedNotes()
        parsed.feed(response.content.decode())
        return parsed

    def note_paragraphs(self, parsed):
        return [(index, node) for index, node in enumerate(parsed.nodes)
                if node['tag'] == 'p' and 'shift-note' in node['attrs'].get('class', '').split()]

    def test_notes_use_accessible_card_trigger_and_hidden_tooltip_without_extra_buttons(self):
        note = 'Вопросы гостю\nВключить запись интервью после новостей.'
        self.shift(note)
        self.shift('', hour=12)
        self.shift(' \n\t ', hour=14)
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        parsed = self.parsed(response)
        cards = parsed.marked('data-shift-note')
        self.assertEqual(len(cards), 1, 'blank or whitespace-only notes do not make a card interactive')
        card_index, card = cards[0]
        self.assertEqual(card['tag'], 'div')
        self.assertIn('shift-entry', card['attrs']['class'].split())
        triggers = parsed.marked('data-shift-note-trigger')
        self.assertEqual(len(triggers), 1)
        trigger_index, trigger = triggers[0]
        self.assertEqual(trigger['tag'], 'div')
        self.assertTrue(parsed.within(trigger_index, card_index))
        self.assertEqual(trigger['attrs']['role'], 'button')
        self.assertEqual(trigger['attrs']['tabindex'], '0')
        self.assertEqual(trigger['attrs']['aria-expanded'], 'false')
        self.assertIn('Показать', trigger['attrs']['aria-label'])
        self.assertIn(self.reporter.first_name, trigger['text'])
        self.assertNotIn(note, trigger['text'], 'the note is not shown as inline card content')
        popups = parsed.marked('data-shift-note-popup')
        self.assertEqual(len(popups), 1)
        popup_index, popup = popups[0]
        self.assertTrue(parsed.within(popup_index, card_index))
        self.assertEqual(popup['parent'], trigger['parent'], 'content trigger and popup are sibling elements')
        self.assertEqual(popup['attrs']['role'], 'tooltip')
        self.assertIn('hidden', popup['attrs'])
        self.assertEqual(parsed.marked('data-shift-note-close'), [])
        self.assertNotContains(response, 'shift-note-toggle')
        self.assertNotContains(response, 'shift-note-header')
        paragraphs = self.note_paragraphs(parsed)
        self.assertEqual(len(paragraphs), 1)
        self.assertTrue(parsed.within(paragraphs[0][0], popup_index))
        self.assertEqual(paragraphs[0][1]['text'], note)

    def test_reporter_can_read_note_popup_without_receiving_edit_controls(self):
        note = 'Редакционная заметка для сотрудника'
        self.shift(note)
        self.client.force_login(self.reporter)
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, note)
        self.assertContains(response, 'data-shift-note-popup')
        self.assertNotContains(response, 'data-shift-actions')
        parsed = self.parsed(response)
        self.assertEqual(self.note_paragraphs(parsed)[0][1]['text'], note)

    def test_literal_html_and_multiline_notes_stay_safe_in_calendar_saved_preview_and_draft(self):
        saved = '<img src=x onerror=alert(1)>\nСохранённое примечание'
        draft = '</script><script>alert("notes-popup")</script>\nПлан нового эфира'
        self.shift(saved)
        response = self.client.get(self.calendar_url, {'date': self.day.isoformat()})
        self.assertEqual(self.note_paragraphs(self.parsed(response))[0][1]['text'], saved)
        self.assertNotContains(response, '<img src=x onerror=alert(1)>')
        response = self.client.post(reverse('newsroom:shift_create') + f'?date={self.day.isoformat()}', {
            'employee': self.reporter.pk, 'date': self.day.isoformat(),
            'start_time': '16:00', 'end_time': '13:00', 'notes': draft,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('end_time', response.context['form'].errors)
        parsed = self.parsed(response)
        self.assertEqual([node['text'] for _, node in self.note_paragraphs(parsed)], [saved, draft])
        popups = parsed.marked('data-shift-note-popup')
        self.assertEqual(len(popups), 2)
        for paragraph_index, _ in self.note_paragraphs(parsed):
            self.assertTrue(any(parsed.within(paragraph_index, popup_index) for popup_index, _ in popups))
        for _, popup in popups:
            self.assertIn('hidden', popup['attrs'])
            self.assertEqual(popup['attrs']['role'], 'tooltip')
        self.assertEqual(len(parsed.marked('data-shift-note-trigger')), 2)
        self.assertEqual(parsed.marked('data-shift-note-close'), [])
        self.assertNotContains(response, '<img src=x onerror=alert(1)>')
        self.assertNotContains(response, '<script>alert("notes-popup")</script>')
        self.assertEqual(BroadcastShift.objects.count(), 1)
