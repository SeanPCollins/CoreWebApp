from contextlib import nullcontext
from datetime import timedelta
import re
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from django.test import override_settings

from apps.registry.models import (
    DataGatheringRun,
    Identifier,
    ProviderTaskLog,
    RawSubstanceInput,
    SearchTask,
    SearchTerm,
    SearchTermReview,
    Source,
    SourceImport,
    Structure,
    StructureComponent,
    StructureCurationBatch,
    StructureCurationBatchFinding,
    StructureEvidence,
    StructureIdentifier,
    Synonym,
    WebFinding,
    WebSource,
    WebSource,
)
from apps.registry.services import structure_curation
from apps.registry.services.base import BaseProvider
from apps.registry.services.extractor import parse_cas_provider
from apps.registry.services.providers.cactus import CactusProvider
from apps.registry.services.parser import commit_parsed_import
from apps.registry.services.task_generator import (
    _is_valid_source_pairing,
    _representations_for_source,
)
from apps.registry.admin import IdentifierAdminForm, SearchTaskAdmin
from apps.registry.tasks import (
    collect_structure_curation_preview,
    process_source_import,
)


@override_settings(
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    DEFAULT_FROM_EMAIL='noreply@example.test',
    ADMIN_EMAIL='admin@example.test',
)
class AccountWorkflowTests(TestCase):
    def test_missing_username_gets_an_available_numbered_username(self):
        user_model = get_user_model()
        user_model.objects.create_user(username='jdoe', password='TestPass!42')
        user_model.objects.create_user(username='jdoe2', password='TestPass!42')

        response = self.client.post(reverse('signup'), {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'email': 'jane@example.test',
            'password1': 'TestPass!42',
            'password2': 'TestPass!42',
            'reason': 'Research access',
        })

        self.assertRedirects(response, reverse('thanks'))
        self.assertTrue(user_model.objects.filter(username='jdoe3').exists())

    def test_signup_creates_pending_user_and_notifies_user_and_admin(self):
        response = self.client.post(reverse('signup'), {
            'first_name': 'Jane',
            'last_name': 'Doe',
            'username': 'jdoe',
            'email': 'jane@example.test',
            'password1': 'TestPass!42',
            'password2': 'TestPass!42',
            'reason': 'Research access',
        })

        self.assertRedirects(response, reverse('thanks'))
        user = get_user_model().objects.get(username='jdoe')
        self.assertFalse(user.is_active)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('Research access', mail.outbox[0].body)
        self.assertIn('activate', mail.outbox[0].body)
        self.assertIn('wait while your account is verified', mail.outbox[1].body)

    def test_activation_shows_copyable_approval_and_login_link(self):
        user = get_user_model().objects.create_user(
            username='jdoe',
            email='jane@example.test',
            password='TestPass!42',
            first_name='Jane',
            is_active=False,
        )
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)

        response = self.client.get(reverse('activate', args=(uid, token)))

        user.refresh_from_db()
        self.assertTrue(user.is_active)
        self.assertContains(response, 'Hello Jane')
        self.assertContains(response, 'Your EDiT account has been approved')
        self.assertContains(response, reverse('login'))

    def test_password_reset_accepts_username_and_sends_reset_link(self):
        user = get_user_model().objects.create_user(
            username='jdoe',
            email='jane@example.test',
            password='TestPass!42',
            is_active=True,
        )

        response = self.client.post(
            reverse('password_reset'),
            {'email': user.username},
        )

        self.assertRedirects(response, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn('password-reset/confirm/', mail.outbox[0].body)
        reset_url = re.search(r'http://testserver\S+', mail.outbox[0].body).group(0)
        confirm_response = self.client.get(reset_url, follow=True)
        self.assertEqual(confirm_response.status_code, 200)
        self.assertContains(confirm_response, 'Choose a new password')
        self.client.post(confirm_response.request['PATH_INFO'], {
            'new_password1': 'NewPass!73',
            'new_password2': 'NewPass!73',
        })
        user.refresh_from_db()
        self.assertTrue(user.check_password('NewPass!73'))

    def test_user_admin_action_sends_reset_link_to_selected_user(self):
        admin_user = get_user_model().objects.create_superuser(
            username='siteadmin',
            email='admin@example.test',
            password='AdminPass!42',
        )
        user = get_user_model().objects.create_user(
            username='jdoe',
            email='jane@example.test',
            password='TestPass!42',
            is_active=True,
        )
        self.client.force_login(admin_user)

        response = self.client.post(
            reverse('admin:auth_user_changelist'),
            {
                'action': 'send_password_reset_links',
                '_selected_action': [str(user.pk)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn('password-reset/confirm/', mail.outbox[0].body)

    def test_user_change_page_can_send_a_password_reset_without_changing_password(self):
        admin_user = get_user_model().objects.create_superuser(
            username='siteadmin',
            email='admin@example.test',
            password='AdminPass!42',
        )
        user = get_user_model().objects.create_user(
            username='jdoe',
            email='jane@example.test',
            password='TestPass!42',
            is_active=True,
        )
        self.client.force_login(admin_user)

        change_page = self.client.get(
            reverse('admin:auth_user_change', args=(user.pk,))
        )
        self.assertContains(change_page, 'Send password reset link')
        reset_page = self.client.get(
            reverse('admin:auth_user_send_password_reset', args=(user.pk,))
        )
        self.assertContains(reset_page, user.email)
        self.assertContains(reset_page, 'does not reveal or change')
        self.assertEqual(len(mail.outbox), 0)

        response = self.client.post(
            reverse('admin:auth_user_send_password_reset', args=(user.pk,)),
            follow=True,
        )

        user.refresh_from_db()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [user.email])
        self.assertIn('password-reset/confirm/', mail.outbox[0].body)
        self.assertTrue(user.check_password('TestPass!42'))

    def test_homepage_displays_live_database_statistics(self):
        source = WebSource.objects.create(
            name='Test provider',
            provider_code='test_provider',
        )
        term = SearchTerm.objects.create(term_value='formaldehyde', term_type='NAME')
        SearchTask.objects.create(
            search_term=term,
            web_source=source,
            target_representation='detail',
            status=SearchTask.TaskStatus.SUCCESS_FOUND,
            is_completed=True,
        )
        SearchTask.objects.create(
            search_term=term,
            web_source=source,
            target_representation='pug_view',
            status=SearchTask.TaskStatus.PENDING,
        )
        Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            molecular_formula='C2H6O',
        )
        Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
            molecular_formula='C2H6NaO',
        )

        response = self.client.get(reverse('home'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['database_stats'], {
            'search_terms': 1,
            'search_tasks': 2,
            'successful_tasks': 1,
            'unique_structures': 2,
        })
        self.assertContains(response, 'CORE at a glance')
        self.assertContains(response, 'Successful provider searches')
        self.assertContains(response, 'Unique chemical structures')


class ChemicalLookupTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='chemical-lookup-user',
            password='test-password',
        )
        self.client.force_login(self.user)
        self.lookup_url = reverse('search')
        self.mapping_url = reverse('chemical_lookup_map')

    def test_pasted_rows_export_matches_and_not_found_rows_as_csv(self):
        import csv
        from io import StringIO

        parent = Structure.objects.create(
            inchikey='ABCDABCDABCDAB-ABCDEFGHIJ-A',
            inchi='InChI=1S/CH4/h1H4',
            molecular_formula='CH4',
        )
        structure = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            inchi='InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
            canonical_smiles='CCO',
            isomeric_smiles='CCO',
            molecular_formula='C2H6O',
            molecular_weight=46.069,
            exact_mass=46.0418648,
            parent=parent,
        )
        identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.CAS,
            identifier_value='64-17-5',
        )
        identifier.structures.add(structure)
        Synonym.objects.create(
            structure=structure,
            synonym_value='Ethanol',
            synonym_type=Synonym.SynonymType.COMMON,
        )

        stage_response = self.client.post(self.lookup_url, {
            'source_mode': 'paste',
            'pasted_rows': 'CAS,Name\n64-17-5,Ethanol\n00-00-0,No match',
            'paste_has_headers': 'on',
            'delimiter': ',',
        })

        self.assertRedirects(
            stage_response,
            self.mapping_url,
            fetch_redirect_response=False,
        )
        mapping_page = self.client.get(self.mapping_url)
        self.assertEqual(mapping_page.status_code, 200)
        self.assertContains(mapping_page, 'CAS-RN')
        self.assertContains(mapping_page, 'Ethanol')

        response = self.client.post(self.mapping_url, {
            'column_0': 'CAS',
            'column_1': 'NAME',
            'data_sections': ['structure', 'identifiers'],
            'file_format': 'csv',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="chemical-lookup-results.csv"', response['Content-Disposition'])
        rows = list(csv.DictReader(StringIO(response.content.decode('utf-8'))))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['resolution_status'], 'MATCHED')
        self.assertEqual(rows[0]['recommended_inchikey'], structure.inchikey)
        self.assertEqual(rows[0]['molecular_formula'], 'C2H6O')
        self.assertEqual(rows[0]['parent_inchikey'], parent.inchikey)
        self.assertNotIn('parent_inchi', rows[0])
        self.assertEqual(rows[0]['alternative_inchikeys'], '')
        self.assertEqual(rows[0]['input_1_CAS'], '64-17-5')
        self.assertEqual(rows[1]['resolution_status'], 'NOT_FOUND')
        self.assertEqual(rows[1]['confidence_level'], 'UNRESOLVED')
        self.assertEqual(rows[1]['input_2_Name'], 'No match')
        self.assertEqual(
            rows[1]['admin_review_status'],
            'Submitted for administrator review',
        )
        self.assertEqual(SearchTermReview.objects.count(), 2)
        self.assertEqual(SearchTask.objects.count(), 0)
        self.assertEqual(self.client.session.get('chemical_lookup_token'), None)

    def test_uploaded_csv_can_be_mapped_and_downloaded_as_excel(self):
        upload = SimpleUploadedFile(
            'chemicals.csv',
            b'CAS Number,Local Note\n64-17-5,"=SUM(1,1)"\n',
            content_type='text/csv',
        )
        response = self.client.post(self.lookup_url, {
            'source_mode': 'file',
            'file_has_headers': 'on',
            'delimiter': 'auto',
            'input_file': upload,
        })

        self.assertRedirects(
            response,
            self.mapping_url,
            fetch_redirect_response=False,
        )
        mapping_page = self.client.get(self.mapping_url)
        self.assertContains(mapping_page, 'CAS Number')
        self.assertContains(mapping_page, 'Local Note')

        download = self.client.post(self.mapping_url, {
            'column_0': 'CAS',
            'data_sections': ['structure'],
            'file_format': 'xlsx',
        })

        self.assertEqual(download.status_code, 200)
        self.assertIn(
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            download['Content-Type'],
        )
        self.assertTrue(
            download['Content-Disposition'].endswith(
                'filename="chemical-lookup-results.xlsx"'
            )
        )
        from openpyxl import load_workbook

        workbook = load_workbook(
            filename=BytesIO(download.content),
            read_only=True,
            data_only=True,
        )
        rows = list(workbook.active.iter_rows(values_only=True))
        workbook.close()
        self.assertEqual(rows[0][0], 'row_number')
        self.assertEqual(rows[1][1], '64-17-5')
        self.assertEqual(rows[1][2], "'=SUM(1,1)")
        self.assertEqual(rows[1][3], 'NOT_FOUND')

    def test_exact_collapse_download_includes_xlsx_lineage(self):
        from openpyxl import load_workbook

        structure = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            molecular_formula='C2H6O',
        )
        identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='shared-structure',
        )
        identifier.structures.add(structure)

        stage_response = self.client.post(self.lookup_url, {
            'source_mode': 'paste',
            'pasted_rows': (
                'Identifier,Source\n'
                'shared-structure,List A\n'
                'shared-structure,List B\n'
            ),
            'paste_has_headers': 'on',
            'delimiter': ',',
        })
        self.assertRedirects(
            stage_response,
            self.mapping_url,
            fetch_redirect_response=False,
        )

        response = self.client.post(self.mapping_url, {
            'column_0': 'OTHER',
            'column_1': '',
            'data_sections': ['structure'],
            'collapse_mode': 'exact',
            'source_list_column': '1',
            'file_format': 'xlsx',
        })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response['Content-Disposition'].endswith(
                'filename="chemical-lookup-collapsed.xlsx"'
            )
        )
        workbook = load_workbook(
            filename=BytesIO(response.content),
            read_only=True,
            data_only=True,
        )
        self.assertEqual(workbook.sheetnames, ['Collapsed results', 'Row lineage'])
        collapsed_rows = list(
            workbook['Collapsed results'].iter_rows(values_only=True)
        )
        lineage_rows = list(workbook['Row lineage'].iter_rows(values_only=True))
        workbook.close()
        self.assertEqual(
            collapsed_rows[1][0],
            f'EXACT-{structure.inchikey}',
        )
        self.assertEqual(collapsed_rows[1][3], 2)
        self.assertEqual(collapsed_rows[1][4], '["List A","List B"]')
        self.assertEqual(len(lineage_rows), 3)
        self.assertEqual(lineage_rows[1][0], lineage_rows[2][0])
        self.assertEqual(lineage_rows[1][3:5], ('List A', 1))
        self.assertEqual(lineage_rows[2][3:5], ('List B', 2))
        self.assertEqual(lineage_rows[1][-2], 'shared-structure')
        self.assertEqual(lineage_rows[1][-1], 'List A')

    def test_parent_collapse_uses_parent_structure_and_keeps_each_source_row(self):
        parent = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            molecular_formula='C2H6O',
            canonical_smiles='CCO',
        )
        salt_component = Structure.objects.create(
            inchikey='FKNQFGJONOIPTF-UHFFFAOYSA-N',
            molecular_formula='Na',
        )
        salt = Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
            molecular_formula='C2H6NaO',
            parent=parent,
        )
        StructureComponent.objects.create(
            structure=salt,
            component=parent,
            position=0,
            is_parent_component=True,
        )
        StructureComponent.objects.create(
            structure=salt,
            component=salt_component,
            position=1,
            is_parent_component=False,
        )

        from apps.registry.services.chemical_lookup import (
            export_collapsed_rows,
            resolve_chemical_rows,
        )

        payload = resolve_chemical_rows(
            [
                {'column_0': parent.inchikey, 'column_1': 'Source A'},
                {'column_0': salt.inchikey, 'column_1': 'Source B'},
            ],
            {'0': 'INCHIKEY'},
            ['structure'],
        )
        collapsed_headers, collapsed_rows, lineage_headers, lineage_rows = (
            export_collapsed_rows(
                payload,
                ['InChIKey', 'Source'],
                'parent',
                '1',
            )
        )

        self.assertEqual(len(collapsed_rows), 1)
        self.assertEqual(
            collapsed_rows[0]['collapsed_group_id'],
            f'PARENT-{parent.inchikey}',
        )
        self.assertEqual(collapsed_rows[0]['recommended_inchikey'], parent.inchikey)
        self.assertEqual(collapsed_rows[0]['molecular_formula'], 'C2H6O')
        self.assertEqual(collapsed_rows[0]['member_row_count'], 2)
        self.assertEqual(
            collapsed_rows[0]['source_lists'],
            '["Source A","Source B"]',
        )
        self.assertEqual(len(lineage_rows), 2)
        self.assertEqual(
            {row['source_list'] for row in lineage_rows},
            {'Source A', 'Source B'},
        )
        self.assertIn('source_row_number', lineage_headers)
        self.assertIn('collapse_key', collapsed_headers)

    def test_lookup_accepts_16000_input_rows(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary_directory:
            input_path = Path(temporary_directory) / 'large-list.csv'
            input_path.write_text(
                'Identifier\n' + ''.join(
                    f'identifier-{index}\n' for index in range(16000)
                ),
                encoding='utf-8',
            )
            from apps.registry.services.chemical_lookup import read_input_file

            headers, input_rows = read_input_file(
                input_path,
                has_headers=True,
                delimiter=',',
            )
        self.assertEqual(headers, ['Identifier'])
        self.assertEqual(len(input_rows), 16000)

    def test_csv_collapse_download_contains_separate_results_and_lineage_files(self):
        import csv
        from io import StringIO
        from zipfile import ZipFile

        structure = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            molecular_formula='C2H6O',
        )
        identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='csv-collapse-id',
        )
        identifier.structures.add(structure)
        self.client.post(self.lookup_url, {
            'source_mode': 'paste',
            'pasted_rows': 'Identifier,Source\ncsv-collapse-id,List A\n',
            'paste_has_headers': 'on',
            'delimiter': ',',
        })

        response = self.client.post(self.mapping_url, {
            'column_0': 'OTHER',
            'data_sections': ['structure'],
            'collapse_mode': 'exact',
            'source_list_column': '1',
            'file_format': 'csv',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')
        with ZipFile(BytesIO(response.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    'chemical-lookup-collapsed.csv',
                    'chemical-lookup-lineage.csv',
                },
            )
            collapsed = list(csv.DictReader(
                StringIO(archive.read('chemical-lookup-collapsed.csv').decode())
            ))
            lineage = list(csv.DictReader(
                StringIO(archive.read('chemical-lookup-lineage.csv').decode())
            ))
        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]['member_row_count'], '1')
        self.assertEqual(len(lineage), 1)
        self.assertEqual(lineage[0]['source_list'], 'List A')

    def test_unmatched_lookup_values_wait_for_admin_approval_before_task_creation(self):
        from apps.registry.services.chemical_lookup import resolve_chemical_rows
        from apps.registry.services.search_term_review import (
            accept_search_term_reviews,
            is_checksum_valid_cas,
            queue_unmatched_search_terms,
        )

        source = WebSource.objects.create(
            name='CAS Common Chemistry',
            provider_code='cas_common',
            is_active=True,
        )
        payload = resolve_chemical_rows(
            [{
                'column_0': '50-00-0',
                'column_1': 'A',
                'column_2': 'C',
            }],
            {'0': 'CAS', '1': 'NAME', '2': 'SMILES'},
            ['structure'],
        )

        queued_rows = queue_unmatched_search_terms(payload, self.user)

        self.assertEqual(queued_rows, 1)
        self.assertEqual(SearchTermReview.objects.count(), 3)
        self.assertEqual(SearchTerm.objects.count(), 0)
        self.assertEqual(SearchTask.objects.count(), 0)
        self.assertEqual(
            payload['results'][0]['admin_review_status'],
            'Submitted for administrator review',
        )
        cas_review = SearchTermReview.objects.get(term_type='CAS')
        self.assertTrue(is_checksum_valid_cas(cas_review))
        accepted = accept_search_term_reviews(
            SearchTermReview.objects.filter(pk=cas_review.pk)
        )

        self.assertEqual(accepted, 1)
        term = SearchTerm.objects.get(term_value='50-00-0')
        self.assertEqual(term.term_type, 'CAS')
        task = SearchTask.objects.get(search_term=term, web_source=source)
        self.assertEqual(task.status, SearchTask.TaskStatus.PENDING)
        self.assertFalse(task.is_completed)
        self.assertEqual(SearchTermReview.objects.count(), 2)

        approved_payload = resolve_chemical_rows(
            [{'column_0': '50-00-0'}],
            {'0': 'CAS'},
            ['structure'],
        )
        queue_unmatched_search_terms(approved_payload, self.user)
        self.assertEqual(
            approved_payload['results'][0]['admin_review_status'],
            'Already approved as a search term',
        )
        self.assertEqual(SearchTermReview.objects.count(), 2)
        self.assertEqual(SearchTask.objects.filter(search_term=term).count(), 1)

        rejected_count, _details = SearchTermReview.objects.filter(
            term_value='A'
        ).delete()
        self.assertEqual(rejected_count, 1)
        self.assertFalse(SearchTermReview.objects.filter(term_value='A').exists())
        self.assertFalse(SearchTerm.objects.filter(term_value='A').exists())

    def test_inconsistent_identifier_matches_are_reported_as_conflicts(self):
        first = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        second = Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
        )
        identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='ambiguous-id',
        )
        identifier.structures.add(first, second)

        from apps.registry.services.chemical_lookup import (
            export_rows,
            resolve_chemical_rows,
        )

        result = resolve_chemical_rows(
            [{'column_0': 'ambiguous-id'}],
            {'0': 'OTHER'},
            ['structure'],
        )

        self.assertEqual(result['schema_version'], '1.3')
        self.assertEqual(result['row_count'], 1)
        self.assertEqual(result['results'][0]['resolution']['status'], 'CONFLICT')
        self.assertEqual(
            result['results'][0]['resolution']['recommended_inchikey'],
            first.inchikey,
        )
        self.assertEqual(result['results'][0]['structure']['inchikey'], first.inchikey)
        self.assertEqual(
            result['results'][0]['alternatives'],
            [second.inchikey],
        )
        self.assertEqual(result['results'][0]['resolution']['selection_tied'], True)
        self.assertNotIn('matched_inputs', result['results'][0]['resolution'])
        self.assertNotIn('unmatched_inputs', result['results'][0]['resolution'])
        headers, exported = export_rows(result, ['Ambiguous ID'], 'csv')
        self.assertIn('recommended_inchikey', headers)
        self.assertIn('alternative_inchikeys', headers)
        self.assertNotIn('matched_inputs', headers)
        self.assertNotIn('unmatched_inputs', headers)
        self.assertEqual(exported[0]['alternative_inchikeys'], second.inchikey)

    def test_candidate_with_more_supporting_inputs_is_recommended(self):
        first = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        second = Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
        )
        first_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='first-id',
        )
        first_identifier.structures.add(first)
        second_first_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='second-first-id',
        )
        second_first_identifier.structures.add(first)
        other_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='other-id',
        )
        other_identifier.structures.add(second)

        from apps.registry.services.chemical_lookup import resolve_chemical_rows

        result = resolve_chemical_rows(
            [{
                'column_0': 'first-id',
                'column_1': 'second-first-id',
                'column_2': 'other-id',
            }],
            {'0': 'OTHER', '1': 'OTHER', '2': 'OTHER'},
            ['structure'],
        )

        resolved = result['results'][0]
        self.assertEqual(resolved['resolution']['status'], 'CONFLICT')
        self.assertEqual(resolved['structure']['inchikey'], first.inchikey)
        self.assertEqual(resolved['resolution']['matched_input_count'], 3)
        self.assertEqual(resolved['alternatives'], [second.inchikey])

    def test_stereochemistry_variants_are_grouped_as_same_connectivity(self):
        parent = Structure.objects.create(
            inchikey='ITRJWOMZKQRYTA-UHFFFAOYSA-N',
            molecular_formula='C10H20',
            isomeric_smiles='CC(O)C',
        )
        first = Structure.objects.create(
            inchikey='ITRJWOMZKQRYTA-RFZYENFJSA-N',
            molecular_formula='C10H20',
            isomeric_smiles='C[C@H](O)C',
            parent=parent,
        )
        second = Structure.objects.create(
            inchikey='ITRJWOMZKQRYTA-UHFFFAOYSA-P',
            molecular_formula='C10H20',
            isomeric_smiles='CC(O)C',
            parent=parent,
        )

        from apps.registry.services.chemical_lookup import resolve_chemical_rows

        result = resolve_chemical_rows(
            [{'column_0': first.inchikey, 'column_1': second.inchikey}],
            {'0': 'INCHIKEY', '1': 'INCHIKEY'},
            ['structure'],
        )

        resolved = result['results'][0]
        self.assertEqual(resolved['resolution']['status'], 'STEREO_VARIANT')
        self.assertEqual(resolved['resolution']['matched_input_count'], 2)
        self.assertEqual(resolved['structure']['inchikey'], first.inchikey)
        self.assertEqual(resolved['structure']['parent__inchikey'], parent.inchikey)
        self.assertEqual(resolved['alternatives'], [])

    def test_salt_variant_status_uses_recorded_non_parent_component(self):
        parent = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            molecular_formula='C2H6O',
        )
        salt_component = Structure.objects.create(
            inchikey='FKNQFGJONOIPTF-UHFFFAOYSA-N',
            molecular_formula='Na',
        )
        salt = Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
            molecular_formula='C2H6NaO',
            parent=parent,
        )
        StructureComponent.objects.create(
            structure=salt,
            component=parent,
            position=0,
            is_parent_component=True,
        )
        StructureComponent.objects.create(
            structure=salt,
            component=salt_component,
            position=1,
            is_parent_component=False,
        )

        from apps.registry.services.chemical_lookup import resolve_chemical_rows

        result = resolve_chemical_rows(
            [{'column_0': parent.inchikey, 'column_1': salt.inchikey}],
            {'0': 'INCHIKEY', '1': 'INCHIKEY'},
            ['structure'],
        )

        resolved = result['results'][0]
        self.assertEqual(resolved['resolution']['status'], 'SALT_VARIANT')
        self.assertEqual(resolved['resolution']['parent_inchikey'], parent.inchikey)
        self.assertEqual(resolved['structure']['inchikey'], parent.inchikey)

    def test_systematic_name_and_cas_findings_contribute_support_and_conflicts(self):
        favored = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        alternative = Structure.objects.create(
            inchikey='QUSNBJAOOMFDIB-UHFFFAOYSA-N',
        )
        systematic = SearchTerm.objects.create(
            term_value='Formaldehyde',
            term_type='SYSTEMATIC',
        )
        cas_term = SearchTerm.objects.create(
            term_value='50-00-0',
            term_type='CAS',
        )
        cas_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.CAS,
            identifier_value='50-00-0',
        )
        cas_identifier.structures.add(favored)

        for index, (term, structure) in enumerate(
            [
                *((systematic, favored) for _ in range(6)),
                *((systematic, alternative) for _ in range(2)),
                *((cas_term, favored) for _ in range(2)),
                *((cas_term, alternative) for _ in range(1)),
            ]
        ):
            source = WebSource.objects.create(
                name=f'Lookup source {index}',
                provider_code=f'lookup-{index}',
            )
            task, _ = SearchTask.objects.get_or_create(
                search_term=term,
                web_source=source,
            )
            finding = WebFinding.objects.create(
                search_task=task,
                raw_payload='{}',
            )
            StructureEvidence.objects.create(
                structure=structure,
                web_finding=finding,
                assessment=StructureEvidence.Assessment.SUPPORTS,
            )

        from apps.registry.services.chemical_lookup import resolve_chemical_rows

        result = resolve_chemical_rows(
            [{'column_0': 'Formaldehyde', 'column_1': '50-00-0'}],
            {'0': 'NAME', '1': 'CAS'},
            ['structure'],
        )

        resolved = result['results'][0]
        self.assertEqual(resolved['structure']['inchikey'], favored.inchikey)
        self.assertEqual(resolved['resolution']['supporting_finding_count'], 8)
        self.assertEqual(resolved['resolution']['conflicting_finding_count'], 3)
        self.assertEqual(resolved['resolution']['supporting_search_task_count'], 8)
        self.assertEqual(resolved['resolution']['conflicting_search_task_count'], 3)
        self.assertEqual(resolved['resolution']['supporting_provider_count'], 8)
        self.assertEqual(resolved['resolution']['confidence_level'], 'MODERATE')
        self.assertEqual(
            resolved['resolution']['evidence_summary'],
            '8 of 11 tasks from 8 providers; 3 conflicts',
        )
        self.assertEqual(resolved['alternatives'], [alternative.inchikey])

    def test_provider_detail_evidence_follows_name_search_chains(self):
        import json

        structure = Structure.objects.create(
            inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        input_term = SearchTerm.objects.create(
            term_value='Shared chemical name',
            term_type='SYSTEMATIC',
        )
        alternate_input_term = SearchTerm.objects.create(
            term_value='Alternate chemical name',
            term_type='COMMON',
        )
        chains = [
            ('pubchem', 'CID', '12345', 'pug_view', {'resolved_cid': '12345'}),
            (
                'comptox',
                'DTXSID',
                'DTXSID123456',
                'detail',
                {'resolved_dtxsid': 'DTXSID123456'},
            ),
            (
                'cas_common',
                'CAS',
                '50-00-0',
                'detail',
                {'rn': '50-00-0'},
            ),
            (
                'chembl',
                'CHEMBL_ID',
                'CHEMBL123',
                'default',
                {'resolved_chembl_ids': ['CHEMBL123']},
            ),
        ]

        for index, (provider, id_type, identifier, detail_rep, payload) in enumerate(chains):
            source = WebSource.objects.create(
                name=f'{provider} chain source',
                provider_code=provider,
            )
            root_representation = 'default' if provider == 'chembl' else 'search'
            root_task, _ = SearchTask.objects.get_or_create(
                search_term=input_term,
                web_source=source,
                target_representation=root_representation,
            )
            WebFinding.objects.create(
                search_task=root_task,
                raw_payload=json.dumps(payload),
            )
            alternate_root_task, _ = SearchTask.objects.get_or_create(
                search_term=alternate_input_term,
                web_source=source,
                target_representation=root_representation,
            )
            WebFinding.objects.create(
                search_task=alternate_root_task,
                raw_payload=json.dumps(payload),
            )
            detail_term = SearchTerm.objects.create(
                term_value=identifier,
                term_type=id_type,
            )
            detail_task, _ = SearchTask.objects.get_or_create(
                search_term=detail_term,
                web_source=source,
                target_representation=detail_rep,
            )
            detail_finding = WebFinding.objects.create(
                search_task=detail_task,
                raw_payload='{}',
            )
            StructureEvidence.objects.create(
                structure=structure,
                web_finding=detail_finding,
                assessment=StructureEvidence.Assessment.SUPPORTS,
            )

        from apps.registry.services.chemical_lookup import resolve_chemical_rows

        result = resolve_chemical_rows(
            [{
                'column_0': 'Shared chemical name',
                'column_1': 'Alternate chemical name',
            }],
            {'0': 'NAME', '1': 'NAME'},
            ['structure'],
        )

        resolution = result['results'][0]['resolution']
        self.assertEqual(resolution['recommended_inchikey'], structure.inchikey)
        self.assertEqual(resolution['supporting_finding_count'], 4)
        self.assertEqual(resolution['supporting_search_task_count'], 8)
        self.assertEqual(resolution['supporting_provider_count'], 4)
        self.assertEqual(resolution['evidence_summary'], '8 of 8 tasks from 4 providers')


class ProcessSourceImportTaskTests(SimpleTestCase):
    @patch('apps.registry.tasks.commit_parsed_import')
    @patch('apps.registry.tasks.generate_staged_preview', return_value=[])
    @patch('apps.registry.tasks.transaction.atomic', return_value=nullcontext())
    @patch('apps.registry.tasks.SourceImport.objects.select_for_update')
    @patch('apps.registry.tasks.SourceImport.objects.filter')
    def test_processes_pending_import_from_persisted_metadata(
        self,
        _filter_queryset,
        select_for_update,
        atomic,
        generate_staged_preview,
        commit_parsed_import,
    ):
        source_import = SimpleNamespace(
            import_status=SourceImport.ImportStatus.PENDING,
            original_filename='input.csv',
            file_path='/media/imports/abc123_input.csv',
            file_encoding='utf-8-sig',
            column_mapping={'CAS': {'category': 'IDENTIFIER', 'subtype': 'CAS'}},
            chemical_group_id=None,
            chemical_group=None,
            file_hash='abc123',
            save=MagicMock(),
            progress_message='',
        )
        select_for_update.return_value.get.return_value = source_import

        result = process_source_import.run('import-id')

        self.assertEqual(result, 'import completed')
        generate_staged_preview.assert_called_once_with(
            '/media/imports/abc123_input.csv',
            'utf-8-sig',
            source_import.column_mapping,
        )
        commit_parsed_import.assert_called_once()
        source_import.save.assert_called_once_with(
            update_fields=[
                'import_status',
                'progress_message',
                'error_log',
            ]
        )


class CactusRoutingTests(SimpleTestCase):
    def test_masked_identifiers_are_excluded_from_all_provider_queues(self):
        term = SimpleNamespace(
            term_type='Masked_ID',
            term_value='masked-123',
        )

        self.assertFalse(_is_valid_source_pairing(term, 'pubchem'))

    def test_masked_ids_are_not_sent_to_cactus(self):
        term = SimpleNamespace(
            term_type='Masked ID',
            term_value='190811',
        )

        self.assertFalse(_is_valid_source_pairing(term, 'cactus'))
        self.assertEqual(_representations_for_source(term, 'cactus'), ())

    def test_systematic_names_keep_all_cactus_representations(self):
        term = SimpleNamespace(
            term_type='SYSTEMATIC',
            term_value='Example chemical name',
        )

        self.assertEqual(
            _representations_for_source(term, 'cactus'),
            ('stdinchikey', 'smiles', 'iupac_name'),
        )

    def test_cas_common_uses_polite_pacing_and_long_retry_fallback(self):
        from apps.registry.services.providers.cas_common import CasCommonProvider

        self.assertEqual(CasCommonProvider.min_request_interval, 1.5)
        self.assertEqual(CasCommonProvider.default_retry_after_seconds, 15 * 60)

    @patch.object(CactusProvider, 'cancellation_requested', return_value=False)
    @patch.object(CactusProvider, 'handle_task_not_found')
    @patch.object(CactusProvider, 'polite_throttle')
    def test_cactus_encodes_slashes_in_structure_inputs(
        self,
        polite_throttle,
        handle_task_not_found,
        _cancellation_requested,
    ):
        provider = CactusProvider()
        response = SimpleNamespace(status_code=404)
        provider.session.get = MagicMock(return_value=response)
        task = SimpleNamespace(
            search_term=SimpleNamespace(term_value='C/C'),
            web_source=SimpleNamespace(
                base_url='https://cactus.example/chemical/structure',
            ),
            target_representation='smiles',
            last_run_at=None,
        )
        task_log = SimpleNamespace(
            processed_items=0,
            success_count=0,
            not_found_count=0,
            error_count=0,
            save=MagicMock(),
        )

        provider.execute_batch([task], task_log)

        self.assertEqual(
            provider.session.get.call_args.args[0],
            'https://cactus.example/chemical/structure/C%2FC/smiles',
        )
        polite_throttle.assert_called_once()
        handle_task_not_found.assert_called_once()

    @patch('apps.registry.services.base.WebFinding.objects.update_or_create')
    def test_exhausted_transient_errors_are_not_classified_as_not_found(
        self,
        update_or_create,
    ):
        class TestProvider(BaseProvider):
            def execute_batch(self, tasks, task_log, *args, **kwargs):
                pass

        provider = TestProvider()
        task = SimpleNamespace(
            run_count=2,
            status='PROCESSING',
            is_completed=False,
            error_message='',
            save=MagicMock(),
        )
        task_log = SimpleNamespace(error_count=0, not_found_count=0)

        provider.handle_task_error(
            task,
            'https://example.test/lookup',
            'HTTP Error 500',
            task_log,
        )

        self.assertEqual(task.status, 'ERROR')
        self.assertTrue(task.is_completed)
        self.assertIn('Retries exhausted', task.error_message)
        self.assertEqual(task_log.error_count, 1)
        self.assertEqual(task_log.not_found_count, 0)
        update_or_create.assert_called_once()


class WebFindingSourceUrlTests(TestCase):
    def test_source_url_accepts_long_provider_request_urls(self):
        source = WebSource.objects.create(
            name='Long URL test provider',
            provider_code='long-url-test',
        )
        term = SearchTerm.objects.create(
            term_value='long search term',
            term_type='SYSTEMATIC',
        )
        task = SearchTask.objects.create(
            search_term=term,
            web_source=source,
        )
        source_url = 'https://example.test/' + ('x' * 1200)

        finding = WebFinding.objects.create(
            search_task=task,
            source_url=source_url,
            raw_payload='{}',
        )

        self.assertEqual(finding.source_url, source_url)


class MaskedIdentifierImportTests(TestCase):
    def test_masked_identifiers_are_stored_without_becoming_search_terms(self):
        source = WebSource.objects.create(
            name='Masked identifier import provider',
            provider_code='masked-test',
            is_active=True,
        )
        source_import = commit_parsed_import(
            file_hash='masked-id-import',
            file_path='/tmp/masked-id-import.csv',
            original_filename='masked-id-import.csv',
            staged_rows=[{
                'row_index': 1,
                'raw_json': '{"Masked ID": "MASK-123", "Name": "Example name"}',
                'primary_id': 'MASK-123',
                'primary_type': 'Masked ID',
                'cas_validation': None,
            }],
            mapping={
                'Masked ID': {
                    'category': 'IDENTIFIER',
                    'subtype': 'Masked ID',
                },
                'Name': {
                    'category': 'SYNONYM',
                    'subtype': 'NAME',
                },
            },
        )

        identifier = Identifier.objects.get(
            identifier_type=Identifier.IdentifierType.MASKED_ID,
            identifier_value='MASK-123',
        )
        name_term = SearchTerm.objects.get(term_value='Example name')
        self.assertFalse(SearchTerm.objects.filter(term_value='MASK-123').exists())
        self.assertFalse(
            SearchTask.objects.filter(search_term__term_value='MASK-123').exists()
        )
        self.assertTrue(
            SearchTask.objects.filter(
                search_term=name_term,
                web_source=source,
            ).exists()
        )

        raw_row = source_import.raw_rows.get(row_index=1)
        masked_input = raw_row.substance_inputs.get(input_value='MASK-123')
        name_input = raw_row.substance_inputs.get(input_value='Example name')
        self.assertEqual(masked_input.identifier, identifier)
        self.assertIsNone(masked_input.search_term)
        self.assertEqual(name_input.search_term, name_term)
        self.assertEqual(
            {
                term_value
                for term_value in raw_row.substance_inputs.values_list(
                    'search_term__term_value',
                    flat=True,
                )
                if term_value
            },
            {'Example name'},
        )


class CactusUnsupportedTaskTests(TestCase):
    def test_masked_identifier_tasks_are_skipped_without_a_provider_request(self):
        from apps.registry.services.provider_batch import run_provider_batch

        source = WebSource.objects.create(
            name='NCI CACTUS',
            provider_code='cactus',
            is_active=True,
        )
        term = SearchTerm.objects.create(
            term_value='190811',
            term_type='Masked ID',
        )
        task = SearchTask.objects.create(
            search_term=term,
            web_source=source,
            target_representation='smiles',
            status=SearchTask.TaskStatus.PENDING,
        )
        previously_misclassified_term = SearchTerm.objects.create(
            term_value='50-00-0',
            term_type='CAS',
        )
        previously_misclassified_task = SearchTask.objects.create(
            search_term=previously_misclassified_term,
            web_source=source,
            target_representation='stdinchikey',
            status=SearchTask.TaskStatus.NOT_FOUND,
            run_count=3,
            is_completed=True,
            error_message='Max retries (3) reached. Failed with: HTTP Error 500',
        )

        _task_log, claimed_count, has_more = run_provider_batch(
            provider_code='cactus',
            batch_size=10,
        )

        task.refresh_from_db()
        self.assertEqual(task.status, SearchTask.TaskStatus.SKIPPED)
        self.assertTrue(task.is_completed)
        self.assertIn('masked identifiers', task.error_message)
        self.assertEqual(claimed_count, 0)
        self.assertFalse(has_more)
        previously_misclassified_task.refresh_from_db()
        self.assertEqual(
            previously_misclassified_task.status,
            SearchTask.TaskStatus.ERROR,
        )
        self.assertTrue(previously_misclassified_task.is_completed)


class DataGatheringRunTaskTests(TestCase):
    def test_rate_limit_backoff_uses_retry_after_and_grows_to_cap(self):
        from apps.registry.tasks import rate_limit_backoff_seconds

        self.assertEqual(rate_limit_backoff_seconds(1, 60), 15 * 60)
        self.assertEqual(rate_limit_backoff_seconds(2, 60), 30 * 60)
        self.assertEqual(rate_limit_backoff_seconds(3, 3 * 60 * 60), 3 * 60 * 60)
        self.assertEqual(rate_limit_backoff_seconds(5, 60), 2 * 60 * 60)

    def test_rate_limited_run_is_requeued_with_progressive_backoff(self):
        from apps.registry.services.base import ProviderRateLimited
        from apps.registry.tasks import process_data_gathering_run

        run = DataGatheringRun.objects.create(
            provider_code='cas_common',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=10,
        )
        with (
            patch('apps.registry.tasks.connection.close'),
            patch(
                'apps.registry.tasks.run_provider_batch',
                side_effect=ProviderRateLimited(60),
            ),
            patch('apps.registry.tasks.process_data_gathering_run.apply_async') as apply_async,
        ):
            result = process_data_gathering_run.run(str(run.pk))
            self.assertEqual(result, 'rate limited; retry queued after 900s')
            apply_async.assert_called_once()
            self.assertEqual(apply_async.call_args.kwargs['countdown'], 900)

        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.RUNNING)
        self.assertEqual(run.rate_limit_retries, 1)

        with (
            patch('apps.registry.tasks.connection.close'),
            patch(
                'apps.registry.tasks.run_provider_batch',
                side_effect=ProviderRateLimited(60),
            ),
            patch('apps.registry.tasks.process_data_gathering_run.apply_async') as apply_async,
        ):
            process_data_gathering_run.run(str(run.pk))
            self.assertEqual(apply_async.call_args.kwargs['countdown'], 1800)

        run.refresh_from_db()
        self.assertEqual(run.rate_limit_retries, 2)

    @patch('apps.registry.tasks.connection.close')
    @patch('apps.registry.tasks.run_provider_batch')
    @patch('apps.registry.tasks.process_data_gathering_run.apply_async')
    def test_manual_pause_during_batch_prevents_queueing_next_batch(
        self,
        apply_async,
        run_provider_batch,
        _connection_close,
    ):
        from apps.registry.tasks import process_data_gathering_run

        run = DataGatheringRun.objects.create(
            provider_code='cactus',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=10,
        )
        def pause_during_batch(**_kwargs):
            DataGatheringRun.objects.filter(pk=run.pk).update(
                status=DataGatheringRun.Status.PAUSED,
            )
            return SimpleNamespace(processed_items=5), 5, True

        run_provider_batch.side_effect = pause_during_batch

        result = process_data_gathering_run.run(str(run.pk))

        self.assertEqual(result, 'paused after current batch')
        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.PAUSED)
        self.assertEqual(run.processed_count, 5)
        apply_async.assert_not_called()

    @patch('apps.registry.tasks.connection.close')
    @patch('apps.registry.tasks.run_provider_batch')
    @patch('apps.registry.tasks.process_data_gathering_run.apply_async')
    def test_cancellation_during_batch_prevents_queueing_next_batch(
        self,
        apply_async,
        run_provider_batch,
        _connection_close,
    ):
        from apps.registry.tasks import process_data_gathering_run

        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=10,
        )
        task_log = ProviderTaskLog.objects.create(
            provider='pubchem',
            status='CANCELLED',
            total_items=10,
            processed_items=3,
        )

        def cancel_during_batch(**_kwargs):
            DataGatheringRun.objects.filter(pk=run.pk).update(
                status=DataGatheringRun.Status.CANCELLED,
            )
            return task_log, 10, True

        run_provider_batch.side_effect = cancel_during_batch

        result = process_data_gathering_run.run(str(run.pk))

        self.assertEqual(result, 'cancelled after current batch')
        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.CANCELLED)
        self.assertEqual(run.processed_count, 3)
        apply_async.assert_not_called()

    @patch('apps.registry.tasks.connection.close')
    @patch('apps.registry.tasks.run_provider_batch')
    def test_batch_failure_marks_run_failed_with_actual_reason(
        self,
        run_provider_batch,
        _connection_close,
    ):
        from apps.registry.tasks import process_data_gathering_run

        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=10,
        )
        run_provider_batch.side_effect = RuntimeError(
            "PubChem detail lookup failed."
        )

        with self.assertRaisesMessage(
            RuntimeError,
            "PubChem detail lookup failed.",
        ):
            process_data_gathering_run.run(str(run.pk))

        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.FAILED)
        self.assertIn("PubChem detail lookup failed.", run.pause_reason)

    def test_pubchem_pug_view_error_uses_provider_error_handler(self):
        from apps.registry.services.providers.pubchem import PubChemProvider

        source = WebSource.objects.create(
            name='PubChem',
            provider_code='pubchem',
            is_active=True,
        )
        term = SearchTerm.objects.create(term_value='123', term_type='CID')
        task = SearchTask.objects.create(
            search_term=term,
            web_source=source,
            target_representation='pug_view',
            status=SearchTask.TaskStatus.PROCESSING,
        )
        task_log = ProviderTaskLog.objects.create(
            provider='pubchem',
            status='RUNNING',
            total_items=1,
        )
        provider = PubChemProvider()
        with patch.object(
            provider.session,
            'get',
            side_effect=RuntimeError('request failed'),
        ):
            provider.execute_batch([task], task_log)

        task.refresh_from_db()
        task_log.refresh_from_db()
        self.assertEqual(task.status, SearchTask.TaskStatus.ERROR)
        self.assertEqual(task.error_message, 'request failed')
        self.assertEqual(task_log.error_count, 1)

    def test_provider_batch_stops_after_cancelled_task_log(self):
        from apps.registry.services.provider_batch import run_provider_batch
        from apps.registry.services.providers.pubchem import PubChemProvider

        source = WebSource.objects.create(
            name='PubChem',
            provider_code='pubchem',
            is_active=True,
        )
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=2,
        )
        tasks = []
        for value in ('first-term', 'second-term'):
            term = SearchTerm.objects.create(term_value=value, term_type='NAME')
            tasks.append(SearchTask.objects.create(
                search_term=term,
                web_source=source,
            ))

        def finish_first_task_and_cancel(task, task_log):
            task.status = SearchTask.TaskStatus.SUCCESS_FOUND
            task.is_completed = True
            task.save(update_fields=['status', 'is_completed'])
            ProviderTaskLog.objects.filter(pk=task_log.pk).update(
                status='CANCELLED',
            )
            DataGatheringRun.objects.filter(pk=run.pk).update(
                status=DataGatheringRun.Status.CANCELLED,
            )

        with patch.object(
            PubChemProvider,
            '_execute_search_task',
            side_effect=finish_first_task_and_cancel,
        ) as execute_task:
            task_log, processed_count, _has_more = run_provider_batch(
                provider_code='pubchem',
                batch_size=2,
                run_id=run.pk,
            )

        self.assertEqual(execute_task.call_count, 1)
        self.assertEqual(task_log.status, 'CANCELLED')
        self.assertEqual(processed_count, 1)
        tasks[0].refresh_from_db()
        tasks[1].refresh_from_db()
        task_statuses = {task.status for task in tasks}
        self.assertEqual(
            task_statuses,
            {
                SearchTask.TaskStatus.SUCCESS_FOUND,
                SearchTask.TaskStatus.PENDING,
            },
        )


class ImportProgressViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.client.force_login(
            user_model.objects.create_user(
                username='import-progress-test',
                password='test-password',
                is_staff=True,
                is_superuser=True,
            )
        )
        source = Source.objects.create(source_name='large-import.csv')
        self.source_import = SourceImport.objects.create(
            source=source,
            original_filename='large-import.csv',
            staging_status=SourceImport.StagingStatus.READY,
            staged_row_count=28000,
        )

    @patch('apps.registry.views.process_source_import.apply_async')
    def test_commit_redirects_to_progress_page(self, apply_async):
        response = self.client.post(
            reverse('import_commit', args=[self.source_import.pk]),
        )

        self.source_import.refresh_from_db()
        self.assertRedirects(
            response,
            reverse('import_status', args=[self.source_import.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(
            self.source_import.import_status,
            SourceImport.ImportStatus.QUEUED,
        )
        status_response = self.client.get(response.url)
        self.assertEqual(status_response.status_code, 200)
        self.assertContains(status_response, '28000')
        apply_async.assert_called_once_with(
            args=[str(self.source_import.pk)],
            queue='registry',
        )

    def test_status_endpoint_reports_staged_rows_and_import_state(self):
        self.source_import.import_status = SourceImport.ImportStatus.PROCESSING
        self.source_import.progress_message = 'Saving records.'
        self.source_import.save(update_fields=['import_status', 'progress_message'])

        response = self.client.get(
            reverse('import_status_data', args=[self.source_import.pk]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                'staging_status': SourceImport.StagingStatus.READY,
                'import_status': SourceImport.ImportStatus.PROCESSING,
                'staged_rows': 28000,
                'row_count': 0,
                'progress_message': 'Saving records.',
                'error': '',
            },
        )

    @patch('apps.registry.views.process_source_import.apply_async')
    def test_queued_import_cannot_be_queued_twice(self, apply_async):
        self.source_import.import_status = SourceImport.ImportStatus.QUEUED
        self.source_import.save(update_fields=['import_status'])

        response = self.client.post(
            reverse('import_commit', args=[self.source_import.pk]),
        )

        self.assertRedirects(
            response,
            reverse('import_status', args=[self.source_import.pk]),
            fetch_redirect_response=False,
        )
        apply_async.assert_not_called()


class DescriptionAndInstructionsViewTests(TestCase):
    def test_description_describes_sources_and_information_types(self):
        response = self.client.get(reverse('description'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'CAS Common Chemistry')
        self.assertContains(response, 'PubChem')
        self.assertContains(response, 'CAS Registry Numbers')
        self.assertContains(response, 'InChIKey')
        self.assertNotContains(response, 'Staff instructions')
        self.assertNotContains(response, 'Needs work')

    def test_instructions_page_is_staff_only_and_reachable_from_staff_menu(self):
        response = self.client.get(reverse('instructions'))
        self.assertEqual(response.status_code, 302)

        user_model = get_user_model()
        staff_user = user_model.objects.create_user(
            username='instructions-staff-test',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(staff_user)

        response = self.client.get(reverse('instructions'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Import a dataset')
        self.assertContains(response, 'Gather information from providers')
        self.assertContains(response, 'Review structure findings')
        self.assertContains(response, reverse('instructions'))


class IdentifierAdminFormTests(TestCase):
    def test_one_identifier_record_can_link_multiple_structures(self):
        first = Structure.objects.create(
            inchikey='AAAAAAAAAAAAAA-BBBBBBBBBB-C',
        )
        second = Structure.objects.create(
            inchikey='CCCCCCCCCCCCCC-DDDDDDDDDD-E',
        )
        cas_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.CAS,
            identifier_value='58-64-0',
        )

        cas_identifier.structures.add(first, second)

        self.assertEqual(Identifier.objects.count(), 1)
        self.assertEqual(StructureIdentifier.objects.count(), 2)
        self.assertEqual(
            set(cas_identifier.structures.values_list('pk', flat=True)),
            {first.pk, second.pk},
        )

    def test_identifier_options_load_without_aggregating_uuid_primary_keys(self):
        structure = Structure.objects.create(
            inchikey='AAAAAAAAAAAAAA-BBBBBBBBBB-C',
        )
        cas_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.CAS,
            identifier_value='50-00-0',
        )
        other_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.OTHER,
            identifier_value='50-00-0',
        )
        unii_identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.UNII,
            identifier_value='ABC123',
        )
        cas_identifier.structures.add(structure)
        other_identifier.structures.add(structure)
        unii_identifier.structures.add(structure)
        cache.delete('admin_identifier_options')

        form = IdentifierAdminForm()

        self.assertEqual(
            list(form.fields['identifier_value'].widget.options),
            ['50-00-0', 'ABC123'],
        )
        cache.delete('admin_identifier_options')

    def test_search_task_term_uses_ajax_autocomplete(self):
        self.assertEqual(SearchTaskAdmin.autocomplete_fields, ('search_term',))

    def test_structure_admin_links_by_inchikey_and_uses_parent_lookup(self):
        user_model = get_user_model()
        self.client.force_login(
            user_model.objects.create_user(
                username='structure-admin-test',
                password='test-password',
                is_staff=True,
                is_superuser=True,
            )
        )
        structure = Structure.objects.create(
            inchikey='AAAAAAAAAAAAAA-BBBBBBBBBB-C',
            molecular_formula='C2H6O',
        )

        change_response = self.client.get(
            reverse('admin:registry_structure_change', args=[structure.pk])
        )
        changelist_response = self.client.get(
            reverse('admin:registry_structure_changelist')
        )

        self.assertEqual(change_response.status_code, 200)
        self.assertContains(
            change_response,
            'class="vForeignKeyRawIdAdminField ',
        )
        self.assertNotContains(
            change_response,
            '<select name="parent"',
            html=False,
        )
        self.assertEqual(changelist_response.status_code, 200)
        self.assertContains(
            changelist_response,
            f'>{structure.inchikey}</a>',
            html=False,
        )

    def test_identifier_admin_links_to_each_linked_structure(self):
        user_model = get_user_model()
        self.client.force_login(
            user_model.objects.create_user(
                username='identifier-admin-test',
                password='test-password',
                is_staff=True,
                is_superuser=True,
            )
        )
        structures = [
            Structure.objects.create(inchikey='AAAAAAAAAAAAAA-BBBBBBBBBB-C'),
            Structure.objects.create(inchikey='CCCCCCCCCCCCCC-DDDDDDDDDD-E'),
        ]
        identifier = Identifier.objects.create(
            identifier_type=Identifier.IdentifierType.CAS,
            identifier_value='58-64-0',
        )
        identifier.structures.add(*structures)

        response = self.client.get(
            reverse('admin:registry_identifier_change', args=[identifier.pk])
        )

        self.assertEqual(response.status_code, 200)
        for structure in structures:
            self.assertContains(
                response,
                reverse('admin:registry_structure_change', args=[structure.pk]),
            )
            self.assertContains(response, structure.inchikey)


class IdentifierBackfillTests(TestCase):
    def create_evidence(self, term_value, term_type, structure, assessment):
        source = WebSource.objects.create(
            name=f'Identifier backfill {WebSource.objects.count()}',
            provider_code=f'identifier_backfill_{WebSource.objects.count()}',
        )
        search_term, _created = SearchTerm.objects.get_or_create(
            term_value=term_value,
            defaults={'term_type': term_type},
        )
        task = SearchTask.objects.create(
            search_term=search_term,
            web_source=source,
            target_representation='detail',
        )
        finding = WebFinding.objects.create(
            search_task=task,
            raw_payload='{}',
        )
        StructureEvidence.objects.create(
            structure=structure,
            web_finding=finding,
            assessment=assessment,
        )

    def test_backfill_links_all_supported_structures_for_identifier(self):
        from django.core.management import call_command
        from io import StringIO

        supported = Structure.objects.create(
            inchikey='AAAAAAAAAAAAAA-BBBBBBBBBB-C',
        )
        ambiguous_one = Structure.objects.create(
            inchikey='CCCCCCCCCCCCCC-DDDDDDDDDD-E',
        )
        ambiguous_two = Structure.objects.create(
            inchikey='EEEEEEEEEEEEEE-FFFFFFFFFF-G',
        )
        conflict = Structure.objects.create(
            inchikey='HHHHHHHHHHHHHH-IIIIIIIIII-J',
        )
        self.create_evidence(
            '64-17-5',
            'CAS',
            supported,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            '64-17-5',
            'CAS',
            supported,
            StructureEvidence.Assessment.INDETERMINATE,
        )
        self.create_evidence(
            '75-07-0',
            'CAS',
            supported,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            '75-07-0',
            'CAS',
            ambiguous_two,
            StructureEvidence.Assessment.INDETERMINATE,
        )
        self.create_evidence(
            '50-00-0',
            'CAS',
            ambiguous_one,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            '50-00-0',
            'CAS',
            ambiguous_two,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            '67-56-1',
            'CAS',
            conflict,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            '67-56-1',
            'CAS',
            conflict,
            StructureEvidence.Assessment.CONFLICTS,
        )
        self.create_evidence(
            'ethanol',
            'NAME',
            supported,
            StructureEvidence.Assessment.SUPPORTS,
        )
        self.create_evidence(
            'CID:12345',
            'PubChem_CID',
            supported,
            StructureEvidence.Assessment.SUPPORTS,
        )

        dry_run_output = StringIO()
        call_command(
            'backfill_structure_identifiers',
            stdout=dry_run_output,
        )
        self.assertIn('would be created', dry_run_output.getvalue())
        self.assertFalse(Identifier.objects.exists())

        call_command('backfill_structure_identifiers', '--apply')

        self.assertEqual(Identifier.objects.count(), 5)
        self.assertEqual(
            StructureIdentifier.objects.count(),
            6,
        )
        self.assertEqual(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='50-00-0',
            ).count(),
            1,
        )
        self.assertEqual(
            Identifier.objects.get(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='50-00-0',
            ).structures.count(),
            2,
        )
        self.assertTrue(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='64-17-5',
                structures=supported,
            ).exists()
        )
        self.assertTrue(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='50-00-0',
                structures=ambiguous_one,
            ).exists()
        )
        self.assertTrue(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='50-00-0',
                structures=ambiguous_two,
            ).exists()
        )
        self.assertTrue(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.CAS,
                identifier_value='67-56-1',
                structures=conflict,
            ).exists()
        )
        self.assertTrue(
            Identifier.objects.filter(
                identifier_type=Identifier.IdentifierType.PUBCHEM_CID,
                identifier_value='12345',
                structures=supported,
            ).exists()
        )


class StructureCurationServiceTests(SimpleTestCase):
    def test_cas_parser_keeps_structure_when_inchikey_is_missing(self):
        finding = SimpleNamespace(
            pk='finding-1',
            raw_payload={'smile': 'CCO'},
        )

        records, stats = parse_cas_provider([finding])

        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0]['inchikey'])
        self.assertEqual(records[0]['canonical_smiles'], 'CCO')
        self.assertEqual(stats['success'], 1)

    def test_smiles_can_be_used_to_derive_a_valid_inchikey(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': None,
            'canonical_smiles': 'CCO',
        })

        self.assertIsNone(rejected)
        self.assertEqual(candidate['inchikey_origin'], 'derived')
        self.assertRegex(
            candidate['inchikey'],
            structure_curation.INCHIKEY_PATTERN,
        )

    def test_fractional_salt_formula_and_mass_are_classified_together(self):
        examples = [
            {
                'inchikey': 'SQRSGVMGWSKZTQ-UHFFFAOYSA-N',
                'inchi': (
                    'InChI=1S/C2H6N4O.H2O4S/c3-1(4)6-2(5)7;1-5(2,3)4/'
                    'h(H6,3,4,5,6,7);(H2,1,2,3,4)'
                ),
                'canonical_smiles': 'O=C(N)NC(=N)N.O=S(=O)(O)O',
                'molecular_formula': (
                    'C<sub>2</sub>H<sub>6</sub>N<sub>4</sub>O.'
                    '<sup>1</sup>/<sub>2</sub>H<sub>2</sub>'
                    'O<sub>4</sub>S'
                ),
                'molecular_weight': '302.27',
            },
            {
                'inchikey': 'VJKNNNJQUPOMQS-UHFFFAOYSA-L',
                'inchi': (
                    'InChI=1S/C27H27N2.H2O4S/c1-5-29-23-12-8-6-10-20('
                    '23)21-18-19(14-16-24(21)29)15-17-26-27(2,3)22-11-'
                    '7-9-13-25(22)28(26)4;1-5(2,3)4/h6-18H,5H2,1-4H3;'
                    '(H2,1,2,3,4)/q+1;/p-2'
                ),
                'canonical_smiles': (
                    'O=S(=O)([O-])[O-].C=1C=CC2=C(C1)C3=CC(C=CC3=[N+]'
                    '2CC)=CC=C4N(C=5C=CC=CC5C4(C)C)C'
                ),
                'molecular_formula': (
                    'C<sub>27</sub>H<sub>27</sub>N<sub>2</sub>.'
                    '<sup>1</sup>/<sub>2</sub>O<sub>4</sub>S'
                ),
                'molecular_weight': '855.10',
            },
            {
                'inchikey': 'KJPJKTCATSVXHG-UHFFFAOYSA-N',
                'inchi': (
                    'InChI=1S/Mg.H2O3S/c;1-4(2)3/h;(H2,1,2,3)'
                ),
                'canonical_smiles': '[Mg].O=S(O)O',
                'molecular_formula': (
                    'H<sub>2</sub>O<sub>3</sub>S.'
                    '<sup>1</sup>/<sub>2</sub>Mg'
                ),
                'molecular_weight': '188.47',
            },
        ]

        for example in examples:
            with self.subTest(inchikey=example['inchikey']):
                candidate, rejected = structure_curation.validate_candidate(
                    example,
                )

                self.assertIsNone(rejected)
                self.assertTrue(
                    candidate['validation'][
                        'reported_stoichiometry_mismatch'
                    ],
                )
                self.assertEqual(
                    candidate['validation']['formula'],
                    'mismatch',
                )
                self.assertEqual(
                    candidate['validation']['molecular_weight'],
                    'mismatch',
                )

    def test_stoichiometry_diagnostic_replaces_duplicate_formula_and_mass_flags(
        self,
    ):
        key = 'SQRSGVMGWSKZTQ-UHFFFAOYSA-N'
        group = structure_curation._new_structure_group(key)
        structure_curation._merge_structure_draft(group, {
            'structure': {'inchikey': key},
            'computed_values': [],
            'evidence': {
                'structure_inchikey': key,
                'web_finding_id': 'finding-fractional-salt',
                'provider_code': 'cas_common',
                'source_url': None,
                'finding_updated_at': '2026-01-01T00:00:00+00:00',
                'target_representation': 'detail',
                'assessment': 'SUPPORTS',
                'reported_values': {},
                'validation': {
                    'key_checks': ['structure_key_match'],
                    'reported_stoichiometry_mismatch': True,
                    'computed_values_for_candidate': [{
                        'reported_checks': {
                            'formula': 'mismatch',
                            'molecular_weight': 'mismatch',
                        },
                    }],
                },
            },
        })

        structure, _evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertIn(
            'reported_salt_stoichiometry_mismatch',
            structure['review_flags'],
        )
        self.assertNotIn(
            'reported_formula_mismatch',
            structure['review_flags'],
        )
        self.assertNotIn(
            'reported_molecular_weight_mismatch',
            structure['review_flags'],
        )
        evidence = {
            'provider_code': 'cas_common',
            'assessment': 'SUPPORTS',
            'web_finding_id': 'finding-fractional-salt',
            'reported_values': [{
                'molecular_formula': (
                    'C<sub>2</sub>H<sub>6</sub>N<sub>4</sub>O.'
                    '<sup>1</sup>/<sub>2</sub>H<sub>2</sub>'
                    'O<sub>4</sub>S'
                ),
                'molecular_weight': '302.27',
            }],
            'validation': [{
                'reported_stoichiometry_mismatch': True,
                'computed_values_for_candidate': [{
                    'formula': 'C2H8N4O5S',
                    'molecular_weight': 200.17,
                    'source': 'inchi',
                }],
                'derived_structures': [{
                    'inchi': 'InChI=1S/example',
                    'canonical_smiles': 'N=C(N)NC(=O)N.O=S(=O)(O)O',
                    'source': 'inchi',
                }],
            }],
        }
        details = structure_curation._build_issue_details(
            structure,
            [evidence],
        )
        stoichiometry_issue = next(
            issue for issue in details
            if issue['flag'] == 'reported_salt_stoichiometry_mismatch'
        )
        self.assertEqual(len(stoichiometry_issue['evidence']), 1)
        self.assertTrue(
            any(
                item['field'] == 'molecular_formula'
                and item.get('raw_value')
                for item in stoichiometry_issue['evidence'][0]['reported_values']
            )
        )
        self.assertTrue(
            any(
                item['field'] == 'molecular_formula'
                and item['value'] == 'C2H8N4O5S'
                for item in stoichiometry_issue['evidence'][0]['computed_values']
            )
        )

    def test_fractional_stoichiometry_diagnostic_requires_matching_mass(self):
        candidate, rejected = structure_curation.validate_candidate({
            'canonical_smiles': 'CCO',
            'molecular_formula': 'C.1/2H2',
            'molecular_weight': '999',
        })

        self.assertIsNone(rejected)
        self.assertFalse(
            candidate['validation']['reported_stoichiometry_mismatch'],
        )

    def test_strong_fractional_stoichiometry_evidence_auto_accepts_structure(
        self,
    ):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        key = 'VHFCNZDHPABZJO-UHFFFAOYSA-N'
        finding_id = 'finding-fractional-ratio'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                (run_dir / 'preview.jsonl').write_text(
                    json.dumps({
                        'inchikey': key,
                        'molecular_formula': 'C9H18NO6P',
                        'molecular_weight': 267.216081,
                        'review_flags': [
                            'reported_salt_stoichiometry_mismatch',
                        ],
                    }) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    json.dumps({
                        'structure_inchikey': key,
                        'web_finding_id': finding_id,
                        'assessment': 'SUPPORTS',
                        'validation': [{
                            'key_checks': [
                                'reported_key_valid',
                                'structure_key_match',
                            ],
                            'reported_inchikey': key,
                            'reported_inchikey_valid': True,
                            'computed_inchikeys': [key],
                            'reported_stoichiometry_mismatch': True,
                            'derived_structures': [
                                {
                                    'source': 'inchi',
                                    'inchikey': key,
                                },
                                {
                                    'source': 'canonical_smiles',
                                    'inchikey': key,
                                },
                            ],
                            'computed_values_for_candidate': [
                                {
                                    'source': 'inchi',
                                    'reported_checks': {
                                        'formula': 'mismatch',
                                        'molecular_weight': 'mismatch',
                                    },
                                },
                                {
                                    'source': 'canonical_smiles',
                                    'reported_checks': {
                                        'formula': 'mismatch',
                                        'molecular_weight': 'mismatch',
                                    },
                                },
                            ],
                        }],
                    }) + '\n',
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation
                    ._auto_triage_fractional_stoichiometry_mismatches(
                        run_id,
                    ),
                    [key],
                )
                record = structure_curation.get_preview(run_id)[0]
                self.assertEqual(record['triage_decision'], 'ACCEPTED')
                self.assertIn(
                    'original provider values remain in the evidence',
                    record['triage_auto_reason'],
                )

    def test_fractional_stoichiometry_rule_keeps_indeterminate_evidence_manual(
        self,
    ):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        key = 'VHFCNZDHPABZJO-UHFFFAOYSA-N'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                (run_dir / 'preview.jsonl').write_text(
                    json.dumps({
                        'inchikey': key,
                        'review_flags': [
                            'reported_salt_stoichiometry_mismatch',
                        ],
                    }) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    json.dumps({
                        'structure_inchikey': key,
                        'web_finding_id': 'finding-indeterminate',
                        'assessment': 'INDETERMINATE',
                        'validation': [{
                            'reported_stoichiometry_mismatch': True,
                        }],
                    }) + '\n',
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation
                    ._auto_triage_fractional_stoichiometry_mismatches(
                        run_id,
                    ),
                    [],
                )
                self.assertEqual(
                    structure_curation.get_preview(run_id)[0][
                        'triage_decision'
                    ],
                    'UNDECIDED',
                )

    def test_reported_inchi_composition_rule_rejects_unsupported_smiles(
        self,
    ):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchi_key = 'MQXQVCLAUDMCEF-CWLIKTDRSA-N'
        smiles_key = 'AIPFZZHNBUVELL-UHFFFAOYSA-N'
        finding_id = 'finding-hydrate-tautomer'
        flags = {
            inchi_key: ['conflicting_structure_inputs'],
            smiles_key: [
                'conflicting_structure_inputs',
                'reported_formula_mismatch',
                'reported_molecular_weight_mismatch',
            ],
        }
        derived = [
            {
                'source': 'inchi',
                'inchikey': inchi_key,
                'formula': 'C16H25N3O8S',
            },
            {
                'source': 'canonical_smiles',
                'inchikey': smiles_key,
                'formula': 'C16H21N3O6S',
            },
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
                for run_id, has_independent_support in (
                        (run_ids[0], False),
                        (run_ids[1], True),
                ):
                        state = structure_curation.create_run(run_id)
                        state.update({
                            'status': 'COMPLETED',
                            'report_format_version': (
                                structure_curation.REPORT_FORMAT_VERSION
                            ),
                        })
                        structure_curation._write_state(run_id, state)
                        run_dir = Path(temporary_directory) / run_id
                        structures = [
                            {
                                'inchikey': key,
                                'review_flags': review_flags,
                                'already_in_structure_database': False,
                            }
                            for key, review_flags in flags.items()
                        ]
                        shared_validation = {
                            'key_checks': [
                                'reported_key_valid',
                                'inchi_smiles_disagree',
                                'reported_key_matches_one_structure',
                            ],
                            'reported_inchikey': inchi_key,
                            'reported_inchikey_valid': True,
                            'derived_structures': derived,
                        }
                        evidence = [
                            {
                                'structure_inchikey': inchi_key,
                                'web_finding_id': finding_id,
                                'assessment': 'SUPPORTS',
                                'validation': [{
                                    **shared_validation,
                                    'computed_values_for_candidate': [{
                                        **derived[0],
                                        'reported_checks': {
                                            'formula': 'match',
                                            'molecular_weight': 'match',
                                        },
                                    }],
                                }],
                            },
                            {
                                'structure_inchikey': smiles_key,
                                'web_finding_id': finding_id,
                                'assessment': 'SUPPORTS',
                                'validation': [{
                                    **shared_validation,
                                    'computed_values_for_candidate': [{
                                        **derived[1],
                                        'reported_checks': {
                                            'formula': 'mismatch',
                                            'molecular_weight': 'mismatch',
                                        },
                                    }],
                                }],
                            },
                        ]
                        if has_independent_support:
                            evidence.append({
                                'structure_inchikey': smiles_key,
                                'web_finding_id': 'independent-smiles-support',
                                'assessment': 'SUPPORTS',
                                'validation': [],
                            })
                        (run_dir / 'preview.jsonl').write_text(
                            ''.join(
                                json.dumps(row) + '\n' for row in structures
                            ),
                            encoding='utf-8',
                        )
                        (run_dir / 'structure_evidence.jsonl').write_text(
                            ''.join(
                                json.dumps(row) + '\n' for row in evidence
                            ),
                            encoding='utf-8',
                        )

                self.assertEqual(
                        structure_curation
                        ._auto_triage_reported_inchi_composition_pairs(
                            run_ids[0],
                        ),
                        sorted([inchi_key, smiles_key]),
                )
                triaged = {
                        row['inchikey']: row
                        for row in structure_curation.get_preview(run_ids[0])
                }
                self.assertEqual(
                        triaged[inchi_key]['triage_decision'],
                        'ACCEPTED',
                )
                self.assertEqual(
                        triaged[smiles_key]['triage_decision'],
                        'REJECTED',
                )
                self.assertIn(
                        'no independent finding supports that alternative',
                        triaged[smiles_key]['triage_auto_reason'],
                )
                self.assertEqual(
                        structure_curation
                        ._auto_triage_reported_inchi_composition_pairs(
                            run_ids[1],
                        ),
                        [],
                )
                held_for_review = {
                        row['inchikey']: row
                        for row in structure_curation.get_preview(run_ids[1])
                }
                self.assertEqual(
                        held_for_review[inchi_key]['triage_decision'],
                        'UNDECIDED',
                )

    def test_reported_inchi_counterion_rule_triages_only_the_supported_candidate(
        self,
    ):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchi_key = 'NEMOMHPGEUBIAT-UHFFFAOYSA-N'
        smiles_key = 'GMWFUGWQJCCVHI-UHFFFAOYSA-N'
        finding_id = 'finding-molybdate-counterions'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                structures = [
                    {
                        'inchikey': inchi_key,
                        'review_flags': ['conflicting_structure_inputs'],
                        'already_in_structure_database': False,
                    },
                    {
                        'inchikey': smiles_key,
                        'review_flags': [
                            'conflicting_structure_inputs',
                            'reported_formula_mismatch',
                            'reported_molecular_weight_mismatch',
                        ],
                        'already_in_structure_database': False,
                    },
                ]
                derived_structures = [
                    {
                        'source': 'inchi',
                        'inchikey': inchi_key,
                        'formula': 'MoNa2O4',
                        'molecular_weight': 205.91,
                    },
                    {
                        'source': 'canonical_smiles',
                        'inchikey': smiles_key,
                        'formula': 'MoNaO4-',
                        'molecular_weight': 182.94737,
                    },
                ]
                validation = {
                    'key_checks': ['inchi_smiles_disagree'],
                    'reported_inchikey': inchi_key,
                    'reported_inchikey_valid': True,
                    'derived_structures': derived_structures,
                }
                evidence = [
                    {
                        'structure_inchikey': inchi_key,
                        'web_finding_id': finding_id,
                        'assessment': 'SUPPORTS',
                        'validation': [{
                            **validation,
                            'computed_values_for_candidate': [{
                                **derived_structures[0],
                                'reported_checks': {
                                    'formula': 'match',
                                    'molecular_weight': 'match',
                                },
                            }],
                        }],
                    },
                    {
                        'structure_inchikey': smiles_key,
                        'web_finding_id': finding_id,
                        'assessment': 'SUPPORTS',
                        'validation': [{
                            **validation,
                            'computed_values_for_candidate': [{
                                **derived_structures[1],
                                'reported_checks': {
                                    'formula': 'mismatch',
                                    'molecular_weight': 'mismatch',
                                },
                            }],
                        }],
                    },
                ]
                evidence_by_structure = {
                    inchi_key: [evidence[0]],
                    smiles_key: [evidence[1]],
                }
                self.assertIsNotNone(
                    structure_curation._reported_inchi_counterion_pair(
                        evidence,
                        {row['inchikey']: row for row in structures},
                        evidence_by_structure,
                        finding_id,
                    ),
                )
                evidence_by_structure[smiles_key].append({
                    'web_finding_id': 'independent-support',
                    'assessment': 'SUPPORTS',
                })
                self.assertIsNone(
                    structure_curation._reported_inchi_counterion_pair(
                        evidence,
                        {row['inchikey']: row for row in structures},
                        evidence_by_structure,
                        finding_id,
                    ),
                )
                (run_dir / 'preview.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in structures),
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in evidence),
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation._auto_triage_reported_inchi_counterion_pairs(
                        run_id,
                    ),
                    sorted([inchi_key, smiles_key]),
                )
                decisions = {
                    row['inchikey']: row
                    for row in structure_curation.get_preview(run_id)
                }
                self.assertEqual(
                    decisions[inchi_key]['triage_decision'],
                    'ACCEPTED',
                )
                self.assertEqual(
                    decisions[smiles_key]['triage_decision'],
                    'REJECTED',
                )
                self.assertIn(
                    'reported InChIKey, formula, and molecular weight',
                    decisions[smiles_key]['triage_auto_reason'],
                )
                structure_curation.save_structure_decision(
                    run_id,
                    smiles_key,
                    'ACCEPTED',
                )
                overridden = {
                    row['inchikey']: row
                    for row in structure_curation.get_preview(run_id)
                }
                self.assertEqual(
                    overridden[smiles_key]['triage_decision'],
                    'ACCEPTED',
                )
                self.assertIsNone(
                    overridden[smiles_key]['triage_auto_reason'],
                )

    def test_reported_inchi_counterion_rule_rejects_non_counterion_differences(
        self,
    ):
        inchi_formula = 'C2H6Na2O4S'
        smiles_formula = 'C2H6NaO3S'

        self.assertFalse(
            structure_curation._inchi_formula_has_only_extra_counterions(
                inchi_formula,
                smiles_formula,
            ),
        )

    def test_clear_salt_pairs_are_auto_accepted_but_other_flags_block_them(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchi_key = 'VUHQYAFIMXEKOM-UHFFFAOYSA-N'
        smiles_key = 'NNJNGOPGQSGTBR-UHFFFAOYSA-N'
        second_inchi_key = 'DKEONVNYXODZRQ-UHFFFAOYSA-N'
        second_smiles_key = 'IWRJMQUPCKSBFP-UHFFFAOYSA-N'
        derived_structures = {
            'salt-pair-finding': [
                {
                    'source': 'inchi',
                    'inchikey': inchi_key,
                    'formula': 'C26H19Li4N5O13S4',
                    'isomeric_smiles': 'NCCN.[Li+].[Li+]',
                },
                {
                    'source': 'canonical_smiles',
                    'inchikey': smiles_key,
                    'formula': 'C26H19LiN5O13S4',
                    'isomeric_smiles': 'NCCN.[Li+]',
                },
            ],
            'salt-pair-finding-2': [
                {
                    'source': 'inchi',
                    'inchikey': second_inchi_key,
                    'formula': 'C7H12Cl2N2',
                    'isomeric_smiles': 'NCCN.Cl.Cl',
                },
                {
                    'source': 'canonical_smiles',
                    'inchikey': second_smiles_key,
                    'formula': 'C7H10ClN2',
                    'isomeric_smiles': 'NCCN.Cl',
                },
            ],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                flags_by_key = {
                    inchi_key: ['conflicting_structure_inputs'],
                    smiles_key: [
                        'conflicting_structure_inputs',
                        'reported_formula_mismatch',
                        'reported_molecular_weight_mismatch',
                    ],
                    second_inchi_key: ['conflicting_structure_inputs'],
                    second_smiles_key: [
                        'conflicting_structure_inputs',
                        'reported_formula_mismatch',
                    ],
                }
                structures = [
                    {
                        'inchikey': key,
                        'parent_inchikey': (
                            inchi_key
                            if key in {inchi_key, smiles_key}
                            else second_inchi_key
                        ),
                        'review_flags': flags,
                    }
                    for key, flags in flags_by_key.items()
                ]
                evidence = [
                    {
                        'structure_inchikey': key,
                        'web_finding_id': finding_id,
                        'validation': [{
                            'key_checks': ['inchi_smiles_disagree'],
                            'derived_structures': derived_structures[
                                finding_id
                            ],
                        }],
                    }
                    for finding_id, keys in (
                        (
                            'salt-pair-finding',
                            (inchi_key, smiles_key),
                        ),
                        (
                            'salt-pair-finding-2',
                            (second_inchi_key, second_smiles_key),
                        ),
                    )
                    for key in keys
                ]
                (run_dir / 'preview.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in structures),
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in evidence),
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation._auto_accept_clear_salt_pairs(
                        run_id,
                    ),
                    sorted([
                        inchi_key,
                        smiles_key,
                        second_inchi_key,
                        second_smiles_key,
                    ]),
                )
                self.assertEqual(
                    {
                        row['inchikey']: row['triage_decision']
                        for row in structure_curation.get_preview(run_id)
                    },
                    {
                        inchi_key: 'ACCEPTED',
                        smiles_key: 'ACCEPTED',
                        second_inchi_key: 'ACCEPTED',
                        second_smiles_key: 'ACCEPTED',
                    },
                )
                triage = structure_curation.get_triage_state(run_id)
                triage['structures'][inchi_key]['decision'] = 'UNDECIDED'
                triage['structures'][smiles_key]['decision'] = 'UNDECIDED'
                structure_curation._atomic_json(
                    structure_curation._triage_path(run_id),
                    triage,
                )
                derived_structures['salt-pair-finding'][1]['formula'] = (
                    derived_structures['salt-pair-finding'][0]['formula']
                )
                derived_structures['salt-pair-finding'][1][
                    'isomeric_smiles'
                ] = 'NCCN.[Li+].[Li+]'
                evidence[0]['validation'][0]['derived_structures'] = (
                    derived_structures['salt-pair-finding']
                )
                evidence[1]['validation'][0]['derived_structures'] = (
                    derived_structures['salt-pair-finding']
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in evidence),
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation._auto_accept_clear_salt_pairs(
                        run_id,
                    ),
                    [],
                )
                self.assertEqual(
                    {
                        row['inchikey']: row['triage_decision']
                        for row in structure_curation.get_preview(run_id)
                    },
                    {
                        inchi_key: 'UNDECIDED',
                        smiles_key: 'UNDECIDED',
                        second_inchi_key: 'ACCEPTED',
                        second_smiles_key: 'ACCEPTED',
                    },
                )

                triage = structure_curation.get_triage_state(run_id)
                triage['structures'][inchi_key]['decision'] = 'UNDECIDED'
                triage['structures'][smiles_key]['decision'] = 'UNDECIDED'
                derived_structures['salt-pair-finding'][1]['formula'] = (
                    'C26H19LiN5O13S4'
                )
                derived_structures['salt-pair-finding'][1][
                    'isomeric_smiles'
                ] = 'NCCN.[Li+]'
                evidence[0]['validation'][0]['derived_structures'] = (
                    derived_structures['salt-pair-finding']
                )
                evidence[1]['validation'][0]['derived_structures'] = (
                    derived_structures['salt-pair-finding']
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in evidence),
                    encoding='utf-8',
                )
                structure_curation._atomic_json(
                    structure_curation._triage_path(run_id),
                    triage,
                )
                next(
                    row for row in structures
                    if row['inchikey'] == inchi_key
                )['review_flags'].append(
                    'conflicting_computed_molecular_formula',
                )
                (run_dir / 'preview.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in structures),
                    encoding='utf-8',
                )

                self.assertEqual(
                    structure_curation._auto_accept_clear_salt_pairs(
                        run_id,
                    ),
                    [],
                )
                self.assertEqual(
                    {
                        row['inchikey']: row['triage_decision']
                        for row in structure_curation.get_preview(run_id)
                    },
                    {
                        inchi_key: 'UNDECIDED',
                        smiles_key: 'UNDECIDED',
                        second_inchi_key: 'ACCEPTED',
                        second_smiles_key: 'ACCEPTED',
                    },
                )

    def test_equivalent_smiles_spellings_do_not_conflict(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
            'canonical_smiles': 'OCC',
        })

        self.assertIsNone(rejected)
        self.assertEqual(
            candidate['validation']['derived_structures'][0]['inchikey'],
            candidate['validation']['derived_structures'][1]['inchikey'],
        )
        self.assertNotIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )

    def test_reported_inchikey_prefix_is_removed_before_comparison(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'InChIKey=LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            'canonical_smiles': 'CCO',
        })

        self.assertIsNone(rejected)
        self.assertEqual(
            candidate['reported_inchikey'],
            'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        self.assertNotIn(
            'reported_derived_key_mismatch',
            candidate['validation']['key_checks'],
        )

    def test_conflicting_input_structures_are_detected_by_derived_keys(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'InChIKey=DKEONVNYXODZRQ-UHFFFAOYSA-N',
            'inchi': (
                'InChI=1S/C7H10N2.2ClH/c1-9-7-5-3-2-4-6(7)8;;/'
                'h2-5,9H,8H2,1H3;2*1H'
            ),
            'canonical_smiles': 'Cl.NC=1C=CC=CC1NC',
        })

        self.assertIsNone(rejected)
        self.assertEqual(
            candidate['reported_inchikey'],
            'DKEONVNYXODZRQ-UHFFFAOYSA-N',
        )
        self.assertIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )
        self.assertEqual(
            len(candidate['validation']['derived_structures']),
            2,
        )

    def test_lithium_salt_input_conflict_can_be_split_by_exact_structure_key(
        self,
    ):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'VUHQYAFIMXEKOM-UHFFFAOYSA-N',
            'inchi': (
                'InChI=1S/C26H19N5O13S4.4Li/c27-20-12-18-13(9-23(20)47('
                '39,40)41)10-24(48(42,43)44)25(26(18)32)31-30-22-8-7-21('
                '17-6-5-16(11-19(17)22)46(36,37)38)29-28-14-1-3-15(4-'
                '2-14)45(33,34)35;;;;/h1-12,32H,27H2,(H,33,34,35)(H,36,'
                '37,38)(H,39,40,41)(H,42,43,44);;;;'
            ),
            'canonical_smiles': (
                '[Li].O=S(=O)(O)C1=CC=C(N=NC2=CC=C(N=NC3=C(O)C4=CC(N)='
                'C(C=C4C=C3S(=O)(=O)O)S(=O)(=O)O)C=5C=C(C=CC25)S(=O)(=O)O)'
                'C=C1'
            ),
        })

        self.assertIsNone(rejected)
        self.assertIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )
        structure_rows = {
            derived['inchikey']: {
                'parent_inchikey': 'RPKCLSMBVQLWIN-UHFFFAOYSA-N',
                'review_flags': [
                    'conflicting_structure_inputs',
                    *(
                        [
                            'reported_formula_mismatch',
                            'reported_molecular_weight_mismatch',
                        ]
                        if derived['source'] == 'canonical_smiles'
                        else []
                    ),
                ],
            }
            for derived in candidate['validation']['derived_structures']
        }
        split = structure_curation._split_structure_input_candidates(
            [{
                'web_finding_id': 'lithium-salt-finding',
                'validation': [candidate['validation']],
            }],
            structure_rows,
        )

        self.assertEqual(
            split['keys'],
            [
                'NNJNGOPGQSGTBR-UHFFFAOYSA-N',
                'VUHQYAFIMXEKOM-UHFFFAOYSA-N',
            ],
        )
        structure_rows['NNJNGOPGQSGTBR-UHFFFAOYSA-N'][
            'review_flags'
        ].append('reported_key_mismatch')
        self.assertEqual(
            structure_curation._split_structure_input_candidates(
                [{
                    'web_finding_id': 'lithium-salt-finding',
                    'validation': [candidate['validation']],
                }],
                structure_rows,
            ),
            [],
        )

    def test_nonstandard_inchi_and_smiles_are_compared_in_standard_mode(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'UDZAXLGLNUMCRX-KHPPLWFENA-N',
            'inchi': (
                'InChI=1/C21H41NO2/c1-3-4-5-6-7-8-9-10-11-12-13-14-15-16-'
                '17-18-21(24)22-19-20(2)23/h10-11,20,23H,3-9,12-19H2,'
                '1-2H3,(H,22,24)/b11-10-'
            ),
            'canonical_smiles': (
                'CCCCCCCC\\C=C/CCCCCCCC(=O)NCC(C)O'
            ),
        })

        self.assertIsNone(rejected)
        self.assertNotIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )
        self.assertNotIn(
            'reported_derived_key_mismatch',
            candidate['validation']['key_checks'],
        )
        self.assertEqual(
            candidate['reported_inchikey'],
            'UDZAXLGLNUMCRX-KHPPLWFESA-N',
        )
        self.assertEqual(
            {
                item['inchikey']
                for item in candidate['validation']['derived_structures']
            },
            {'UDZAXLGLNUMCRX-KHPPLWFESA-N'},
        )

    def test_unspecified_smiles_stereochemistry_is_not_a_structure_conflict(self):
        import json
        from datetime import datetime, timezone
        from types import SimpleNamespace

        payload = {
            'inchikeyKey': 'InChIKey=AYRXSINWFIIFAE-GFRRCQKTSA-N',
            'inchi': (
                'InChI=1S/C12H22O11/c13-1-4(15)7(17)8(18)5(16)3-22-12-'
                '11(21)10(20)9(19)6(2-14)23-12/h1,4-12,14-21H,2-3H2/'
                't4-,5+,6+,7+,8+,9-,10-,11+,12-/m0/s1'
            ),
            'smile': 'O=CC(O)C(O)C(O)C(O)COC1OC(CO)C(O)C(O)C1O',
        }
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': payload['inchikeyKey'],
            'inchi': payload['inchi'],
            'canonical_smiles': payload['smile'],
        })

        self.assertIsNone(rejected)
        self.assertEqual(
            candidate['inchikey'],
            'AYRXSINWFIIFAE-GFRRCQKTSA-N',
        )
        self.assertIn(
            'stereo_unspecified',
            candidate['validation']['key_checks'],
        )
        self.assertNotIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )

        finding = SimpleNamespace(
            pk='stereo-unspecified-finding',
            raw_payload=json.dumps(payload),
            source_url='https://example.test/structure',
            updated_at=datetime.now(timezone.utc),
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='cas_common'),
                target_representation='detail',
            ),
            search_task_id='task-id',
        )
        drafts, rejected = structure_curation.build_finding_drafts(finding)

        self.assertEqual(rejected, [])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(
            drafts[0]['structure']['inchikey'],
            'AYRXSINWFIIFAE-GFRRCQKTSA-N',
        )
        self.assertEqual(drafts[0]['evidence']['assessment'], 'SUPPORTS')
        self.assertIn('@', drafts[0]['structure']['isomeric_smiles'])
        self.assertNotIn('@', drafts[0]['structure']['canonical_smiles'])

    def test_unspecified_smiles_stereo_matches_user_reported_inchi(self):
        import json
        from datetime import datetime, timezone
        from types import SimpleNamespace

        payload = {
            'inchikey': 'InChIKey=CAHGCLMLTWQZNJ-BQNIITSRSA-N',
            'inchi': (
                'InChI=1S/C30H50O/c1-20(2)10-9-11-21(3)22-14-18-30(8)'
                '24-12-13-25-27(4,5)26(31)16-17-28(25,6)23(24)15-19-'
                '29(22,30)7/h10,21-22,25-26,31H,9,11-19H2,1-8H3/'
                't21-,22-,25+,26+,28-,29-,30+/m1/s1'
            ),
            'canonical_smiles': (
                'OC1CCC2(C3=C(CCC2C1(C)C)C4(C)CCC(C(C)CCC=C(C)C)C4(C)CC3)C'
            ),
        }
        candidate, rejected = structure_curation.validate_candidate(payload)

        self.assertIsNone(rejected)
        self.assertNotIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )
        self.assertIn(
            'stereo_unspecified',
            candidate['validation']['key_checks'],
        )

        finding = SimpleNamespace(
            pk='steroid-stereo-unspecified-finding',
            raw_payload=json.dumps(payload),
            source_url='https://example.test/structure',
            updated_at=datetime.now(timezone.utc),
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='cas_common'),
                target_representation='detail',
            ),
            search_task_id='task-id',
        )
        drafts, rejected = structure_curation.build_finding_drafts(finding)
        group = structure_curation._new_structure_group(candidate['inchikey'])
        for draft in drafts:
            structure_curation._merge_structure_draft(group, draft)
        structure, _evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertEqual(rejected, [])
        self.assertNotIn(
            'conflicting_structure_inputs',
            structure['review_flags'],
        )

    def test_different_explicit_stereochemistry_is_a_conflict(self):
        from openbabel import pybel

        first = pybel.readstring('smi', 'C[C@H](O)Cl')
        second = pybel.readstring('smi', 'C[C@@H](O)Cl')
        candidate, rejected = structure_curation.validate_candidate({
            'inchi': first.write('inchi').strip(),
            'canonical_smiles': second.write('can').strip().split()[0],
        })

        self.assertIsNone(candidate)
        self.assertEqual(rejected['reason'], 'structure_key_conflict')

    def test_formula_normalization_uses_composition_and_hill_order(self):
        self.assertEqual(
            structure_curation.normalize_formula(' H₆ C₂ O '),
            'C2H6O',
        )
        self.assertEqual(
            structure_curation.normalize_formula(
                'C<sub>2</sub>H<sub>6</sub>O'
            ),
            'C2H6O',
        )
        self.assertEqual(
            structure_curation.normalize_formula('C_{2}H_{6}O'),
            'C2H6O',
        )
        self.assertEqual(
            structure_curation.normalize_formula('C&#8322;H&#8326;O'),
            'C2H6O',
        )
        self.assertEqual(
            structure_curation.normalize_formula('CH3COOH'),
            'C2H4O2',
        )
        self.assertEqual(
            structure_curation.normalize_formula('CuSO4·5H2O'),
            'CuH10O9S',
        )
        self.assertEqual(
            structure_curation.normalize_formula(
                'CuH<sub>2</sub>O<sub>2</sub>'
            ),
            'CuH2O2',
        )
        self.assertEqual(
            structure_curation.normalize_formula(
                'CuH&lt;sub&gt;2&lt;/sub&gt;O&lt;sub&gt;2&lt;/sub&gt;'
            ),
            'CuH2O2',
        )
        variable_formulas = {
            'C<sub>8</sub>H<sub>10</sub>O<sub>4</sub>.xC<sub>5</sub>H<sub>12</sub>O<sub>2</sub>':
                'C13H22O6',
            '(C<sub>4</sub>H<sub>8</sub>O<sub>3</sub>)<sub>x</sub>':
                'C4H8O3',
            '(C<sub>6</sub>H<sub>9</sub>NO.C<sub>4</sub>H<sub>6</sub>O<sub>2</sub>)<sub>x</sub>':
                'C10H15NO3',
            'C<sub>12</sub>H<sub>26</sub>S.(C<sub>7</sub>H<sub>12</sub>O<sub>2</sub>.C<sub>5</sub>H<sub>8</sub>O<sub>2</sub>.C<sub>4</sub>H<sub>6</sub>O<sub>2</sub>)<sub>x</sub>':
                'C28H52O6S',
            '3(C3H6O.C2H4O)x': 'C15H30O6',
            '2(C2H4O)y': 'C4H8O2',
            '(C8H8.C3H8O3.3(C3H6O.C2H4O)x.C3H3N)x':
                'C29H49NO9',
        }
        for reported, normalized in variable_formulas.items():
            with self.subTest(formula=reported):
                self.assertEqual(
                    structure_curation.normalize_formula(reported),
                    normalized,
                )
        fractional_formulas = {
            'C<sub>8</sub>H<sub>8</sub>O<sub>2</sub>.<sup>1</sup>/<sub>2</sub>Ba':
                'C16H16BaO4',
            'H<sub>3</sub>N.<sup>1</sup>/<sub>3</sub>H<sub>3</sub>O<sub>4</sub>P':
                'H12N3O4P',
            '(C<sub>6</sub>H<sub>14</sub>O<sub>3</sub>.C<sub>4</sub>H<sub>10</sub>O<sub>2</sub>.C<sub>4</sub>H<sub>10</sub>O.<sup>1</sup>/<sub>3</sub>Al)<sub>x</sub>.xC<sub>2</sub>H<sub>4</sub>O<sub>2</sub>':
                'C48H114AlO24',
        }
        for reported, normalized in fractional_formulas.items():
            with self.subTest(formula=reported):
                self.assertEqual(
                    structure_curation.normalize_formula(reported),
                    normalized,
                )
        self.assertEqual(
            structure_curation.normalize_formula('C8H4D6'),
            'C8H4D6',
        )
        self.assertEqual(
            structure_curation.normalize_formula('D2O4S'),
            'D2O4S',
        )
        self.assertEqual(
            structure_curation.normalize_formula('NH4+'),
            'H4N',
        )
        self.assertEqual(
            structure_curation.normalize_formula('C27H27N2O4S-'),
            'C27H27N2O4S',
        )

    def test_variable_formula_uses_unit_multiplier_for_comparison(self):
        candidate, rejected = structure_curation.validate_candidate({
            'canonical_smiles': 'CCOC(=O)CO',
            'molecular_formula': '(C<sub>4</sub>H<sub>8</sub>O<sub>3</sub>)<sub>x</sub>',
        })

        self.assertIsNone(rejected)
        self.assertEqual(candidate['normalized_formula'], 'C4H8O3')
        self.assertEqual(candidate['validation']['formula'], 'match')
        self.assertEqual(
            candidate['validation']['derived_structures'][0]
            ['reported_checks']['formula'],
            'match',
        )

    def test_key_only_finding_is_ready_as_an_enrichable_placeholder(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace
        from unittest.mock import patch

        key = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        finding = SimpleNamespace(
            pk='key-only-finding',
            updated_at=datetime.now(timezone.utc),
            source_url='https://example.test/inchikey',
            search_task_id='task-id',
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='test_provider'),
                target_representation='inchikey',
            ),
        )
        with patch(
            'apps.registry.services.structure_curation.parse_finding',
            return_value=[{'inchikey': key}],
        ):
            drafts, rejected = structure_curation.build_finding_drafts(finding)

        self.assertEqual(rejected, [])
        group = structure_curation._new_structure_group(key)
        for draft in drafts:
            structure_curation._merge_structure_draft(group, draft)
        row, _evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertEqual(row['review_flags'], [])
        self.assertTrue(row['key_only_placeholder'])

        existing_values = {
            key: {
                'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                'canonical_smiles': 'CCO',
                'isomeric_smiles': 'CCO',
                'molecular_formula': 'C2H6O',
                'molecular_weight': 46.069,
                'exact_mass': 46.0418648,
                'standardization_toolkit': 'RDKit',
                'standardization_version': 'test',
                'standardization_policy': 'test',
            },
        }
        existing_row, _evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys={key},
            existing_values=existing_values,
        )
        self.assertFalse(existing_row['key_only_placeholder'])
        self.assertEqual(existing_row['canonical_smiles'], 'CCO')
        self.assertEqual(existing_row['molecular_formula'], 'C2H6O')

    def test_copper_formula_is_consistent_across_inchi_and_smiles(self):
        import json
        from datetime import datetime, timezone
        from types import SimpleNamespace

        payload = {
            'inchikeyKey': 'JJLJMEJHUUYSSY-UHFFFAOYSA-L',
            'inchi': 'InChI=1S/Cu.2H2O/h;2*1H2/q+2;;/p-2',
            'smile': 'O[Cu]O',
            'molecularFormula': 'CuH<sub>2</sub>O<sub>2</sub>',
        }
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': payload['inchikeyKey'],
            'inchi': payload['inchi'],
            'canonical_smiles': payload['smile'],
            'molecular_formula': payload['molecularFormula'],
        })

        self.assertIsNone(rejected)
        self.assertEqual(len(candidate['validation']['derived_structures']), 2)
        self.assertEqual(candidate['validation']['formula'], 'match')
        self.assertNotIn(
            'inchi_smiles_disagree',
            candidate['validation']['key_checks'],
        )

        finding = SimpleNamespace(
            pk='copper-formula-finding',
            raw_payload=json.dumps(payload),
            source_url='https://example.test/copper',
            updated_at=datetime.now(timezone.utc),
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='cas_common'),
                target_representation='detail',
            ),
            search_task_id='task-id',
        )
        drafts, rejected = structure_curation.build_finding_drafts(finding)
        group = structure_curation._new_structure_group(
            'JJLJMEJHUUYSSY-UHFFFAOYSA-L',
        )
        for draft in drafts:
            structure_curation._merge_structure_draft(group, draft)
        structure, _evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertEqual(rejected, [])
        self.assertEqual(structure['molecular_formula'], 'CuH2O2')
        self.assertNotIn(
            'conflicting_computed_molecular_formula',
            structure['review_flags'],
        )
        self.assertNotIn(
            'reported_formula_mismatch',
            structure['review_flags'],
        )

    def test_mass_checks_allow_differences_up_to_half_percent(self):
        within_tolerance, rejected = structure_curation.validate_candidate({
            'canonical_smiles': 'CCO',
            'molecular_weight': '46.25',
            'exact_mass': '46.20',
        })
        beyond_tolerance, beyond_rejected = structure_curation.validate_candidate({
            'canonical_smiles': 'CCO',
            'molecular_weight': '46.40',
            'exact_mass': '46.35',
        })

        self.assertIsNone(rejected)
        self.assertIsNone(beyond_rejected)
        self.assertEqual(
            within_tolerance['validation']['molecular_weight'],
            'match',
        )
        self.assertEqual(
            within_tolerance['validation']['exact_mass'],
            'match',
        )
        self.assertEqual(
            beyond_tolerance['validation']['molecular_weight'],
            'mismatch',
        )
        self.assertEqual(
            beyond_tolerance['validation']['exact_mass'],
            'mismatch',
        )
        self.assertTrue(structure_curation._chemistry_values_match(
            'molecular_weight',
            100,
            100.5,
        ))
        self.assertFalse(structure_curation._chemistry_values_match(
            'molecular_weight',
            100,
            100.6,
        ))

    def test_formula_validation_accepts_different_element_order(self):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            'canonical_smiles': 'CCO',
            'molecular_formula': 'H<sub>6</sub>C_{2}O',
        })

        self.assertIsNone(rejected)
        self.assertEqual(candidate['normalized_formula'], 'C2H6O')
        self.assertEqual(candidate['validation']['formula'], 'match')

    def test_parent_generation_uses_rdkit_charge_parent(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.jsonl'
            output_path = Path(temporary_directory) / 'output.jsonl'
            parent_path = Path(temporary_directory) / 'parents.jsonl'
            source_path.write_text(
                json.dumps({
                    'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C',
                    'canonical_smiles': 'CC(=O)[O-].[Na+]',
                    'isomeric_smiles': 'CC(=O)[O-].[Na+]',
                }) + '\n',
                encoding='utf-8',
            )

            structure_curation._run_rdkit_parent_worker(
                source_path,
                output_path,
                parent_path,
            )

            structure = next(structure_curation._iter_jsonl(output_path))
            parent = next(structure_curation._iter_jsonl(parent_path))

        self.assertEqual(structure['parent_generation_status'], 'GENERATED')
        self.assertEqual(parent['molecular_formula'], 'C2H4O2')
        self.assertEqual(parent['standardization_toolkit'], 'RDKit')
        self.assertNotEqual(parent['inchikey'], structure['inchikey'])

    def test_parent_generation_removes_stereochemistry(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.jsonl'
            output_path = Path(temporary_directory) / 'output.jsonl'
            parent_path = Path(temporary_directory) / 'parents.jsonl'
            source_path.write_text(
                '\n'.join(json.dumps(row) for row in (
                    {
                        'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C',
                        'canonical_smiles': 'C[C@H](O)Cl',
                        'isomeric_smiles': 'C[C@H](O)Cl',
                    },
                    {
                        'inchikey': 'CCCCCCCCCCCCCC-DDDDDDDDDD-E',
                        'canonical_smiles': 'C[C@@H](O)Cl',
                        'isomeric_smiles': 'C[C@@H](O)Cl',
                    },
                )) + '\n',
                encoding='utf-8',
            )
            structure_curation._run_rdkit_parent_worker(
                source_path,
                output_path,
                parent_path,
            )
            structures = list(structure_curation._iter_jsonl(output_path))
            parents = list(structure_curation._iter_jsonl(parent_path))

        self.assertEqual(len(parents), 1)
        self.assertEqual(
            structures[0]['parent_inchikey'],
            structures[1]['parent_inchikey'],
        )
        self.assertNotIn('@', parents[0]['isomeric_smiles'])
        self.assertNotIn('/t', parents[0]['inchi'])
        self.assertIn('stereochemistry removed', parents[0]['standardization_policy'])

    def test_multifragment_structure_keeps_components_and_uses_largest_organic_parent(self):
        import json
        import tempfile
        from pathlib import Path

        full_smiles = (
            'N=C(NC(=N)Nc1ccc(cc1)Cl)NCCCCCCNC(=N)NC(=N)'
            'Nc1ccc(cc1)Cl.CC(=O)O'
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            source_path = Path(temporary_directory) / 'source.jsonl'
            output_path = Path(temporary_directory) / 'output.jsonl'
            parent_path = Path(temporary_directory) / 'parents.jsonl'
            source_path.write_text(
                json.dumps({
                    'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C',
                    'canonical_smiles': full_smiles,
                    'isomeric_smiles': full_smiles,
                }) + '\n',
                encoding='utf-8',
            )
            structure_curation._run_rdkit_parent_worker(
                source_path,
                output_path,
                parent_path,
            )
            structure = next(structure_curation._iter_jsonl(output_path))
            parent = next(structure_curation._iter_jsonl(parent_path))

        self.assertEqual(structure['canonical_smiles'], full_smiles)
        self.assertEqual(len(structure['components']), 2)
        self.assertEqual(
            sum(component['component_count'] for component in structure['components']),
            2,
        )
        self.assertEqual(
            sum(component['is_parent_component'] for component in structure['components']),
            1,
        )
        self.assertNotIn('.', parent['canonical_smiles'])
        self.assertEqual(parent['molecular_formula'], 'C22H30Cl2N10')
        self.assertTrue(
            next(
                component for component in structure['components']
                if component['molecular_formula'] == 'C2H4O2'
            )
        )

    def test_structure_draft_keeps_stereochemical_smiles(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        finding = SimpleNamespace(
            pk='finding-stereo',
            raw_payload='{"smile":"C[C@H](O)Cl"}',
            source_url='https://example.test/structure',
            updated_at=datetime.now(timezone.utc),
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='cas_common'),
                target_representation='detail',
            ),
            search_task_id='task-id',
        )

        drafts, rejected = structure_curation.build_finding_drafts(finding)

        self.assertEqual(rejected, [])
        self.assertEqual(len(drafts), 1)
        structure = drafts[0]['structure']
        self.assertNotIn('@', structure['canonical_smiles'])
        self.assertIn('@', structure['isomeric_smiles'])
        self.assertEqual(structure['standardization_toolkit'], 'Open Babel')
        self.assertEqual(
            drafts[0]['evidence']['assessment'],
            'SUPPORTS',
        )

    def test_reported_key_mismatch_creates_conflict_and_computed_candidate(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        finding = SimpleNamespace(
            pk='finding-mismatch',
            raw_payload=(
                '{"inchikeyKey":"AAAAAAAAAAAAAA-BBBBBBBBBB-C",'
                '"smile":"CCO"}'
            ),
            source_url='https://example.test/structure',
            updated_at=datetime.now(timezone.utc),
            search_task=SimpleNamespace(
                web_source=SimpleNamespace(provider_code='cas_common'),
                target_representation='detail',
            ),
            search_task_id='task-id',
        )

        drafts, rejected = structure_curation.build_finding_drafts(finding)
        assessments = {
            draft['structure']['inchikey']: draft['evidence']['assessment']
            for draft in drafts
        }

        self.assertEqual(rejected, [])
        self.assertEqual(
            assessments['AAAAAAAAAAAAAA-BBBBBBBBBB-C'],
            'CONFLICTS',
        )
        self.assertEqual(
            assessments['LFQSCWFLJHTTHZ-UHFFFAOYSA-N'],
            'SUPPORTS',
        )
        conflict = next(
            draft for draft in drafts
            if draft['evidence']['assessment'] == 'CONFLICTS'
        )
        self.assertIsNone(conflict['structure']['canonical_smiles'])

    def test_structure_report_aggregates_fields_and_complete_evidence(self):
        group = structure_curation._new_structure_group(
            'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
        )
        calculated = {
            'inchikey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
            'canonical_smiles': 'CCO',
            'isomeric_smiles': 'CCO',
            'formula': 'C2H6O',
            'molecular_weight': 46.069,
            'exact_mass': 46.0418648,
            'toolkit': 'Open Babel',
            'toolkit_version': 'test',
            'policy': 'test policy',
        }
        for finding_id in ('finding-1', 'finding-2'):
            structure_curation._merge_structure_draft(group, {
                'structure': {
                    'inchikey': calculated['inchikey'],
                },
                'computed_values': [calculated],
                'evidence': {
                    'structure_inchikey': calculated['inchikey'],
                    'web_finding_id': finding_id,
                    'provider_code': 'provider',
                    'source_url': None,
                    'finding_updated_at': '2026-01-01T00:00:00+00:00',
                    'target_representation': 'detail',
                    'assessment': 'SUPPORTS',
                    'reported_values': {'smiles': 'CCO'},
                    'validation': {'key_checks': ['structure_key_match']},
                },
            })

        structure, evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertEqual(structure['molecular_formula'], 'C2H6O')
        self.assertEqual(structure['supporting_finding_count'], 2)
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            {row['web_finding_id'] for row in evidence},
            {'finding-1', 'finding-2'},
        )

    def test_structure_report_handles_missing_smiles_variants(self):
        inchikey = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        group = structure_curation._new_structure_group(inchikey)
        structure_curation._merge_structure_draft(group, {
            'structure': {'inchikey': inchikey},
            'computed_values': [{
                'inchikey': inchikey,
                'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                'canonical_smiles': None,
                'isomeric_smiles': None,
                'formula': 'C2H6O',
                'molecular_weight': 46.069,
                'exact_mass': 46.0418648,
                'toolkit': 'Open Babel',
                'toolkit_version': 'test',
                'policy': 'test policy',
            }],
            'evidence': {
                'structure_inchikey': inchikey,
                'web_finding_id': 'finding-1',
                'provider_code': 'provider',
                'source_url': None,
                'finding_updated_at': '2026-01-01T00:00:00+00:00',
                'target_representation': 'detail',
                'assessment': 'SUPPORTS',
                'reported_values': {},
                'validation': {},
            },
        })

        structure, evidence = structure_curation._serialize_structure_group(
            group,
            existing_keys=set(),
        )

        self.assertIsNone(structure['canonical_smiles'])
        self.assertIsNone(structure['isomeric_smiles'])
        self.assertEqual(len(evidence), 1)

    def test_structure_input_conflict_shows_both_derived_structures(self):
        evidence = {
            'provider_code': 'provider',
            'assessment': 'SUPPORTS',
            'web_finding_id': 'finding-1',
            'reported_values': [{
                'inchi': 'reported-inchi',
                'canonical_smiles': 'reported-smiles',
            }],
            'validation': [{
                'key_checks': ['inchi_smiles_disagree'],
                'derived_structures': [
                    {
                        'source': 'inchi',
                        'inchi': 'computed-from-inchi',
                        'canonical_smiles': 'inchi-smiles',
                        'isomeric_smiles': 'inchi-isomeric-smiles',
                        'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C',
                    },
                    {
                        'source': 'canonical_smiles',
                        'inchi': 'computed-from-smiles',
                        'canonical_smiles': 'smiles-smiles',
                        'isomeric_smiles': 'smiles-isomeric-smiles',
                        'inchikey': 'CCCCCCCCCCCCCC-DDDDDDDDDD-E',
                    },
                ],
                'computed_values_for_candidate': [{
                    'source': 'inchi',
                    'inchi': 'computed-from-inchi',
                    'canonical_smiles': 'inchi-smiles',
                    'isomeric_smiles': 'inchi-isomeric-smiles',
                    'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C',
                }],
            }],
            'structure_errors': [],
        }

        details = structure_curation._build_issue_details(
            {'original_review_flags': ['conflicting_structure_inputs']},
            [evidence],
        )

        issue_evidence = details[0]['evidence'][0]
        self.assertEqual(
            {item['source'] for item in issue_evidence['computed_values']},
            {'inchi', 'canonical_smiles'},
        )
        self.assertIn(
            {
                'field': 'inchi',
                'value': 'computed-from-smiles',
                'source': 'canonical_smiles',
            },
            issue_evidence['computed_values'],
        )

    def test_existing_structure_issue_shows_both_smiles_variants(self):
        record = {
            'original_review_flags': [
                'conflicting_existing_canonical_smiles',
                'conflicting_existing_isomeric_smiles',
            ],
            'computed_value_variants': {
                'canonical_smiles': [
                    {
                        'value': 'CC(=C)C',
                        'finding_count': 1,
                        'finding_ids': ['finding-1'],
                        'providers': ['cas_common'],
                        'sources': ['inchi'],
                    },
                    {
                        'value': 'C=C(C)C',
                        'finding_count': 0,
                        'finding_ids': [],
                        'providers': ['existing Structure'],
                        'source': 'existing Structure',
                    },
                ],
                'isomeric_smiles': [
                    {
                        'value': 'CC(=C)C',
                        'finding_count': 1,
                        'finding_ids': ['finding-1'],
                        'providers': ['cas_common'],
                        'sources': ['inchi'],
                    },
                    {
                        'value': 'C=C(C)C',
                        'finding_count': 0,
                        'finding_ids': [],
                        'providers': ['existing Structure'],
                        'source': 'existing Structure',
                    },
                ],
            },
        }

        details = structure_curation._build_issue_details(record, [])

        self.assertEqual(
            [detail['field'] for detail in details],
            ['canonical_smiles', 'isomeric_smiles'],
        )
        self.assertEqual(
            [variant['value'] for variant in details[0]['variants']],
            ['CC(=C)C', 'C=C(C)C'],
        )
        self.assertEqual(details[0]['variants'][1]['source'], 'existing Structure')

    def test_formula_issue_shows_computed_formula(self):
        evidence = {
            'provider_code': 'cas_common',
            'assessment': 'SUPPORTS',
            'web_finding_id': 'finding-1',
            'reported_values': [{
                'molecular_formula': '(C<sub>4</sub>H<sub>8</sub>)<sub>3</sub>',
            }],
            'validation': [{
                'computed_values_for_candidate': [{
                    'formula': 'C4H8',
                    'reported_checks': {'formula': 'mismatch'},
                }],
            }],
        }

        details = structure_curation._build_issue_details(
            {'original_review_flags': ['reported_formula_mismatch']},
            [evidence],
        )

        issue_evidence = details[0]['evidence'][0]
        self.assertEqual(
            issue_evidence['reported_values'][0]['value'],
            'C12H24',
        )
        self.assertEqual(
            issue_evidence['computed_values'],
            [{'field': 'molecular_formula', 'value': 'C4H8', 'source': None}],
        )

    def test_smiles_comparison_uses_structure_not_string_representation(self):
        self.assertTrue(structure_curation._chemistry_values_match(
            'canonical_smiles',
            'CC(=C)C',
            'C=C(C)C',
        ))
        self.assertTrue(structure_curation._chemistry_values_match(
            'isomeric_smiles',
            'CC(=C)C',
            'C=C(C)C',
        ))
        self.assertFalse(structure_curation._chemistry_values_match(
            'canonical_smiles',
            'CC(=C)C',
            'CC',
        ))
        self.assertFalse(structure_curation._chemistry_values_match(
            'isomeric_smiles',
            'C[C@H](O)Cl',
            'C[C@@H](O)Cl',
        ))
        self.assertTrue(structure_curation._chemistry_values_match(
            'canonical_smiles',
            'C[C@H](O)Cl',
            'C[C@@H](O)Cl',
        ))

    @patch(
        'apps.registry.services.structure_curation._derive_structure_values',
        return_value=(
            [
                {'inchikey': 'AAAAAAAAAAAAAA-BBBBBBBBBB-C'},
                {'inchikey': 'CCCCCCCCCCCCCC-DDDDDDDDDD-E'},
            ],
            [],
        ),
    )
    def test_conflicting_structures_without_reported_key_are_rejected(
        self,
        derive_values,
    ):
        candidate, rejected = structure_curation.validate_candidate({
            'inchikey': None,
            'inchi': 'inchi',
            'canonical_smiles': 'smiles',
        })

        self.assertIsNone(candidate)
        self.assertEqual(rejected['reason'], 'structure_key_conflict')
        self.assertEqual(len(rejected['derived_keys']), 2)

    def test_deleting_preview_preserves_incremental_checkpoint(self):
        import tempfile
        import uuid
        from pathlib import Path

        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                structure_curation.create_run(run_id)
                structure_curation.mark_run_failed(run_id, 'test cleanup')
                checkpoint = Path(temporary_directory) / 'checkpoint.json'
                checkpoint.write_text(
                    '{"updated_at":"2026-01-01T00:00:00+00:00"}',
                    encoding='utf-8',
                )

                structure_curation.delete_run(run_id)

                self.assertTrue(checkpoint.is_file())
                self.assertIsNone(structure_curation.get_latest_run_id())

    def test_full_scan_ignores_saved_checkpoint(self):
        import tempfile
        import uuid
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                (Path(temporary_directory) / 'checkpoint.json').write_text(
                    '{"updated_at":"2026-01-01T00:00:00+00:00"}',
                    encoding='utf-8',
                )

                state = structure_curation.create_run(
                    str(uuid.uuid4()),
                    full_scan=True,
                )

                self.assertEqual(state['scan_mode'], 'full')
                self.assertIsNone(state['start_after'])

    def test_incremental_scan_uses_saved_checkpoint_with_overlap(self):
        import json
        import tempfile
        import uuid
        from datetime import datetime, timezone as datetime_timezone
        from pathlib import Path

        checkpoint_time = datetime(2026, 1, 1, tzinfo=datetime_timezone.utc)
        expected_start = checkpoint_time - structure_curation.OVERLAP
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                (Path(temporary_directory) / 'checkpoint.json').write_text(
                    json.dumps({'updated_at': checkpoint_time.isoformat()}),
                    encoding='utf-8',
                )

                state = structure_curation.create_run(str(uuid.uuid4()))

                self.assertEqual(state['scan_mode'], 'incremental')
                self.assertEqual(
                    datetime.fromisoformat(state['start_after']),
                    expected_start,
                )


class StructureCurationTaskTests(SimpleTestCase):
    @patch('apps.registry.tasks.collect_structure_curation_preview.apply_async')
    @patch('apps.registry.tasks.structure_curation.finalize_scan')
    @patch(
        'apps.registry.tasks.structure_curation.process_scan_chunk',
        return_value=False,
    )
    @patch(
        'apps.registry.tasks.structure_curation.read_run_state',
        return_value={'status': 'RUNNING'},
    )
    @patch(
        'apps.registry.tasks.structure_curation.run_lock',
        return_value=nullcontext(),
    )
    def test_scan_task_queues_next_bounded_chunk(
        self,
        run_lock,
        read_run_state,
        process_scan_chunk,
        finalize_scan,
        apply_async,
    ):
        result = collect_structure_curation_preview.run('run-id')

        self.assertEqual(result, 'structure preview chunk queued')
        process_scan_chunk.assert_called_once_with('run-id')
        finalize_scan.assert_not_called()
        apply_async.assert_called_once_with(
            args=['run-id'],
            queue='registry',
            countdown=1,
        )


class StructureCurationViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='structure-curation-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.url = reverse('structure_curation')

    def test_preview_page_is_staff_only_and_exposes_batch_controls(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Start random batch')
        self.assertContains(response, 'Maximum WebFindings in this random batch')
        self.assertContains(response, 'Process all pending findings')
        self.assertContains(response, 'Previous curation batches')
        self.assertNotContains(response, 'name="action" value="commit"')

    @patch('apps.registry.views.collect_structure_curation_preview.apply_async')
    @patch('apps.registry.views.structure_curation.prepare_finding_batch')
    @patch('apps.registry.views.structure_curation.create_run')
    def test_scan_action_queues_background_task(
        self,
        create_run,
        prepare_finding_batch,
        apply_async,
    ):
        response = self.client.post(
            self.url,
            {'action': 'scan', 'batch_limit': '37'},
        )

        run_id = apply_async.call_args.kwargs['args'][0]
        self.assertRedirects(response, f'{self.url}?run_id={run_id}')
        create_run.assert_called_once_with(run_id, full_scan=False)
        prepare_finding_batch.assert_called_once_with(
            run_id,
            batch_limit='37',
            full_scan=False,
        )
        apply_async.assert_called_once_with(
            args=[run_id],
            queue='registry',
        )

    @patch('apps.registry.views.collect_structure_curation_preview.apply_async')
    @patch('apps.registry.views.structure_curation.prepare_finding_batch')
    @patch('apps.registry.views.structure_curation.create_run')
    def test_full_scan_action_ignores_incremental_checkpoint(
        self,
        create_run,
        prepare_finding_batch,
        apply_async,
    ):
        response = self.client.post(
            self.url,
            {'action': 'full_scan'},
        )

        run_id = apply_async.call_args.kwargs['args'][0]
        self.assertRedirects(response, f'{self.url}?run_id={run_id}')
        create_run.assert_called_once_with(run_id, full_scan=True)
        prepare_finding_batch.assert_called_once_with(
            run_id,
            batch_limit=1000,
            full_scan=True,
        )

    @patch('apps.registry.views.collect_structure_curation_preview.apply_async')
    @patch('apps.registry.views.structure_curation.reprocess_uncommitted_batch')
    @patch('apps.registry.views.structure_curation.create_run')
    def test_reprocess_action_queues_only_previous_batch(
        self,
        create_run,
        reprocess_findings,
        apply_async,
    ):
        import uuid

        previous_run_id = str(uuid.uuid4())
        reprocess_findings.return_value = 12

        response = self.client.post(
            self.url,
            {
                'action': 'reprocess_batch',
                'run_id': previous_run_id,
            },
        )

        new_run_id = apply_async.call_args.kwargs['args'][0]
        self.assertRedirects(response, f'{self.url}?run_id={new_run_id}')
        create_run.assert_called_once_with(new_run_id)
        reprocess_findings.assert_called_once_with(
            previous_run_id,
            new_run_id,
        )
        apply_async.assert_called_once_with(
            args=[new_run_id],
            queue='registry',
        )

    @patch('apps.registry.views.collect_structure_curation_preview.apply_async')
    @patch('apps.registry.views.structure_curation.retry_failed_finalization')
    def test_retry_finalization_action_queues_saved_results(
        self,
        retry_finalization,
        apply_async,
    ):
        import uuid

        run_id = str(uuid.uuid4())
        response = self.client.post(
            self.url,
            {
                'action': 'retry_finalization',
                'run_id': run_id,
            },
        )

        self.assertRedirects(response, f'{self.url}?run_id={run_id}')
        retry_finalization.assert_called_once_with(run_id)
        apply_async.assert_called_once_with(
            args=[run_id],
            queue='registry',
        )

    def test_completed_run_shows_and_downloads_both_draft_reports(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        run_id = str(uuid.uuid4())
        structure_row = {
            'inchikey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
            'canonical_smiles': 'CCO',
            'isomeric_smiles': 'CCO',
            'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
            'molecular_formula': 'C2H6O',
            'molecular_weight': 46.069,
            'exact_mass': 46.0418648,
            'standardization_toolkit': 'Open Babel',
            'standardization_version': '3',
            'standardization_policy': 'test',
            'evidence_count': 1,
            'supporting_finding_count': 1,
            'provider_count': 1,
            'review_status': 'SUPPORTED',
            'review_flags': [],
            'computed_value_variants': {},
            'already_in_structure_database': False,
            'parent_id': None,
        }
        evidence_row = {
            'structure_inchikey': structure_row['inchikey'],
            'web_finding_id': 'finding-1',
            'provider_code': 'cas_common',
            'source_url': 'https://example.test',
            'finding_updated_at': '2026-01-01T00:00:00+00:00',
            'target_representation': 'detail',
            'assessment': 'SUPPORTS',
            'reported_values': [{'canonical_smiles': 'CCO'}],
            'validation': [{'key_checks': ['structure_key_match']}],
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'unique_keys': 1,
                    'evidence_rows': 1,
                    'conflicting_keys': 0,
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                (run_dir / 'preview.jsonl').write_text(
                    json.dumps(structure_row) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    json.dumps(evidence_row) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'parent_structures.jsonl').write_text(
                    json.dumps({
                        **structure_row,
                        'parent_inchikey': structure_row['inchikey'],
                        'parent_is_self': True,
                    }) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'invalid.jsonl').write_text('', encoding='utf-8')

                page = self.client.get(self.url, {'run_id': run_id})
                self.assertContains(page, 'Prospective Structure rows')
                self.assertContains(page, 'Prospective StructureEvidence rows')
                self.assertContains(page, 'Prospective parent Structure rows')
                self.assertContains(page, structure_row['inchikey'])
                self.assertContains(page, 'Open Babel')

                download_url = reverse(
                    'structure_curation_download',
                    args=[run_id, 'evidence'],
                )
                download = self.client.get(download_url)
                self.assertEqual(download.status_code, 200)
                self.assertIn(
                    b'"web_finding_id": "finding-1"',
                    b''.join(download.streaming_content),
                )

    def test_completed_legacy_report_shows_rebuild_notice(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                state = structure_curation.create_run(run_id)
                state['status'] = 'COMPLETED'
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                (run_dir / 'preview.jsonl').write_text(
                    json.dumps({
                        'inchikey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
                        'finding_count': 1,
                    }) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text('', encoding='utf-8')
                (run_dir / 'invalid.jsonl').write_text('', encoding='utf-8')

                page = self.client.get(self.url, {'run_id': run_id})

                self.assertContains(page, 'older report format')
                self.assertContains(page, 'rebuild this batch')
                self.assertNotContains(page, 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N')
                self.assertFalse(
                    structure_curation.has_current_preview_reports(run_id, state)
                )


class StructureCurationScanTests(TestCase):
    def test_failed_complete_run_can_retry_finalization_without_rescanning(self):
        import tempfile
        import uuid
        from pathlib import Path
        from django.utils import timezone

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                provider = WebSource.objects.create(
                    name='Finalization retry provider',
                    provider_code='finalization_retry',
                )
                term = SearchTerm.objects.create(term_value='finalization retry')
                task = SearchTask.objects.create(
                    search_term=term,
                    web_source=provider,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=task,
                    raw_payload='{}',
                )
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'FAILED',
                    'batch_id': None,
                    'total_findings': 1,
                    'processed_findings': 1,
                    'page_number': 1,
                    'expected_report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'upper_bound': timezone.now().isoformat(),
                })
                structure_curation._write_state(run_id, state)
                page_dir = Path(temporary_directory) / run_id / 'pages'
                page_dir.mkdir()
                (page_dir / '00000000.jsonl').write_text('{}\n')

                self.assertTrue(
                    structure_curation.can_retry_failed_finalization(
                        run_id,
                        state,
                    )
                )
                resumed = structure_curation.retry_failed_finalization(
                    run_id,
                )

                self.assertEqual(resumed['status'], 'FINALIZING')
                self.assertEqual(resumed['processed_findings'], 1)
                self.assertEqual(
                    structure_curation.read_run_state(run_id)['status'],
                    'FINALIZING',
                )
                self.assertFalse(
                    StructureCurationBatchFinding.objects.filter(
                        finding=finding,
                    ).exists()
                )

    def test_finalization_retry_restores_original_full_batch_membership(self):
        import tempfile
        import uuid
        from pathlib import Path
        from django.utils import timezone

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                provider = WebSource.objects.create(
                    name='Full batch retry provider',
                    provider_code='full_batch_retry',
                )
                term = SearchTerm.objects.create(term_value='full retry')
                task = SearchTask.objects.create(
                    search_term=term,
                    web_source=provider,
                    target_representation='detail',
                )
                findings = [
                    WebFinding.objects.create(
                        search_task=task,
                        raw_payload='{}',
                    )
                    for _ in range(2)
                ]
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                batch = StructureCurationBatch.objects.create(
                    run_id=run_id,
                    batch_limit=2,
                    random_seed=1,
                    candidate_count=2,
                    selected_count=2,
                    status=StructureCurationBatch.Status.FAILED,
                )
                for finding in findings:
                    StructureCurationBatchFinding.objects.create(
                        finding=finding,
                        status=StructureCurationBatchFinding.Status.PENDING,
                    )
                state.update({
                    'status': 'FAILED',
                    'batch_id': str(batch.pk),
                    'total_findings': 2,
                    'processed_findings': 2,
                    'page_number': 1,
                    'expected_report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'upper_bound': timezone.now().isoformat(),
                })
                structure_curation._write_state(run_id, state)
                page_dir = Path(temporary_directory) / run_id / 'pages'
                page_dir.mkdir()
                (page_dir / '00000000.jsonl').write_text('{}\n')

                structure_curation.retry_failed_finalization(run_id)

                batch.refresh_from_db()
                self.assertEqual(
                    batch.status,
                    StructureCurationBatch.Status.SCANNING,
                )
                self.assertEqual(
                    set(batch.findings.values_list('finding_id', flat=True)),
                    {finding.pk for finding in findings},
                )
                self.assertEqual(
                    set(
                        batch.findings.values_list('status', flat=True)
                    ),
                    {StructureCurationBatchFinding.Status.SELECTED},
                )

    def test_reprocessing_batch_contains_only_uncommitted_original_findings(self):
        import json
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Reprocessing test provider',
                    provider_code='cas_common',
                )
                search_term = SearchTerm.objects.create(
                    term_value='reprocessing test',
                )
                task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                unresolved_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/unresolved',
                    raw_payload='{}',
                )
                unparsed_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/unparsed',
                    raw_payload='{}',
                )
                committed_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/committed',
                    raw_payload='{}',
                )
                committed_needs_work_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/committed-needs-work',
                    raw_payload='{}',
                )
                selected_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/selected',
                    raw_payload='{}',
                )
                rejected_parsed_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/rejected-parsed',
                    raw_payload='{}',
                )
                accepted_uncommitted_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/accepted-uncommitted',
                    raw_payload='{}',
                )
                unrelated_pending_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/new-pending',
                    raw_payload='{}',
                )
                structure = Structure.objects.create(
                    inchikey='LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
                )
                StructureEvidence.objects.create(
                    structure=structure,
                    web_finding=committed_finding,
                    assessment='SUPPORTS',
                    reported_values={},
                    validation={},
                )
                StructureEvidence.objects.create(
                    structure=structure,
                    web_finding=committed_needs_work_finding,
                    assessment='SUPPORTS',
                    reported_values={},
                    validation={},
                )

                old_run_id = str(uuid.uuid4())
                old_batch = StructureCurationBatch.objects.create(
                    run_id=old_run_id,
                    batch_limit=7,
                    random_seed=1,
                    candidate_count=7,
                    selected_count=7,
                    status=StructureCurationBatch.Status.NEEDS_REVIEW,
                )
                unresolved_item = StructureCurationBatchFinding.objects.create(
                    batch=old_batch,
                    finding=unresolved_finding,
                    status=StructureCurationBatchFinding.Status.NEEDS_WORK,
                )
                unparsed_item = StructureCurationBatchFinding.objects.create(
                    batch=old_batch,
                    finding=unparsed_finding,
                    status=StructureCurationBatchFinding.Status.REJECTED,
                )
                committed_item = StructureCurationBatchFinding.objects.create(
                    batch=old_batch,
                    finding=committed_finding,
                    status=StructureCurationBatchFinding.Status.ACCEPTED,
                )
                committed_needs_work_item = (
                    StructureCurationBatchFinding.objects.create(
                        batch=old_batch,
                        finding=committed_needs_work_finding,
                        status=StructureCurationBatchFinding.Status.NEEDS_WORK,
                    )
                )
                selected_item = StructureCurationBatchFinding.objects.create(
                    batch=old_batch,
                    finding=selected_finding,
                    status=StructureCurationBatchFinding.Status.SELECTED,
                )
                rejected_parsed_item = (
                    StructureCurationBatchFinding.objects.create(
                        batch=old_batch,
                        finding=rejected_parsed_finding,
                        status=StructureCurationBatchFinding.Status.REJECTED,
                    )
                )
                accepted_uncommitted_item = (
                    StructureCurationBatchFinding.objects.create(
                        batch=old_batch,
                        finding=accepted_uncommitted_finding,
                        status=StructureCurationBatchFinding.Status.ACCEPTED,
                    )
                )
                state = structure_curation.create_run(old_run_id)
                state.update({
                    'status': 'COMPLETED',
                    'batch_id': str(old_batch.pk),
                })
                structure_curation._write_state(old_run_id, state)
                structure_curation.get_evidence_report_path(
                    old_run_id,
                ).write_text(
                    ''.join(
                        json.dumps({
                            'web_finding_id': str(finding.pk),
                            'structure_inchikey': (
                                'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
                            ),
                        }) + '\n'
                        for finding in (
                            unresolved_finding,
                            committed_finding,
                            committed_needs_work_finding,
                            selected_finding,
                            rejected_parsed_finding,
                            accepted_uncommitted_finding,
                        )
                    ),
                    encoding='utf-8',
                )

                new_run_id = str(uuid.uuid4())
                structure_curation.create_run(new_run_id)
                reprocessed_count = (
                    structure_curation.reprocess_uncommitted_batch(
                        old_run_id,
                        new_run_id,
                    )
                )

                unresolved_item.refresh_from_db()
                unparsed_item.refresh_from_db()
                committed_item.refresh_from_db()
                committed_needs_work_item.refresh_from_db()
                selected_item.refresh_from_db()
                rejected_parsed_item.refresh_from_db()
                accepted_uncommitted_item.refresh_from_db()
                old_batch.refresh_from_db()
                new_batch = StructureCurationBatch.objects.get(
                    run_id=new_run_id,
                )
                new_state = structure_curation.read_run_state(new_run_id)
                self.assertEqual(reprocessed_count, 2)
                self.assertEqual(unresolved_item.batch_id, new_batch.pk)
                self.assertEqual(
                    unresolved_item.status,
                    StructureCurationBatchFinding.Status.SELECTED,
                )
                self.assertEqual(unparsed_item.batch_id, old_batch.pk)
                self.assertEqual(
                    unparsed_item.status,
                    StructureCurationBatchFinding.Status.REJECTED,
                )
                self.assertEqual(committed_item.batch_id, old_batch.pk)
                self.assertEqual(
                    committed_item.status,
                    StructureCurationBatchFinding.Status.ACCEPTED,
                )
                self.assertEqual(
                    committed_needs_work_item.batch_id,
                    old_batch.pk,
                )
                self.assertEqual(
                    committed_needs_work_item.status,
                    StructureCurationBatchFinding.Status.ACCEPTED,
                )
                self.assertEqual(selected_item.batch_id, new_batch.pk)
                self.assertEqual(
                    selected_item.status,
                    StructureCurationBatchFinding.Status.SELECTED,
                )
                self.assertEqual(
                    rejected_parsed_item.batch_id,
                    old_batch.pk,
                )
                self.assertEqual(
                    rejected_parsed_item.status,
                    StructureCurationBatchFinding.Status.REJECTED,
                )
                self.assertEqual(
                    accepted_uncommitted_item.batch_id,
                    old_batch.pk,
                )
                self.assertEqual(
                    accepted_uncommitted_item.status,
                    StructureCurationBatchFinding.Status.ACCEPTED,
                )
                self.assertEqual(
                    old_batch.status,
                    StructureCurationBatch.Status.SUPERSEDED,
                )
                self.assertEqual(new_batch.candidate_count, 2)
                self.assertEqual(new_batch.selected_count, 2)
                self.assertEqual(
                    list(new_batch.findings.values_list('finding_id', flat=True)),
                    [unresolved_finding.pk, selected_finding.pk],
                )
                self.assertEqual(new_state['scan_mode'], 'reprocessed_batch')
                self.assertFalse(
                    hasattr(unrelated_pending_finding, 'curation_queue_item')
                )

    def test_scan_previews_findings_without_database_writes(self):
        import json
        import tempfile
        import uuid
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='CAS Common Chemistry test',
                    provider_code='cas_common',
                )
                search_term = SearchTerm.objects.create(term_value='ethanol test')
                search_task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=search_task,
                    source_url='https://commonchemistry.cas.org/detail?cas_rn=64-17-5',
                    raw_payload=json.dumps({
                        'inchikeyKey': 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
                        'smile': 'CCO',
                        'molecularFormula': 'C<sub>2</sub>H<sub>6</sub>O',
                        'molecularMass': '46.069',
                    }),
                )
                run_id = str(uuid.uuid4())
                structure_curation.create_run(run_id)

                with CaptureQueriesContext(connection) as queries:
                    with structure_curation.run_lock(run_id):
                        self.assertTrue(
                            structure_curation.process_scan_chunk(run_id)
                        )
                        structure_curation.finalize_scan(run_id)

                database_writes = [
                    query['sql']
                    for query in queries
                    if query['sql'].lstrip().upper().startswith(
                        ('INSERT', 'UPDATE', 'DELETE')
                    )
                ]
                self.assertEqual(database_writes, [])
                self.assertEqual(Structure.objects.count(), 0)
                self.assertEqual(WebFinding.objects.get(pk=finding.pk).raw_payload, finding.raw_payload)
                preview = structure_curation.get_preview(run_id)
                self.assertEqual(len(preview), 1)
                self.assertEqual(
                    preview[0]['inchikey'],
                    'LFQSCWFLJHTTHZ-UHFFFAOYSA-N',
                )
                self.assertEqual(preview[0]['canonical_smiles'], 'CCO')
                self.assertEqual(preview[0]['isomeric_smiles'], 'CCO')
                self.assertEqual(preview[0]['molecular_formula'], 'C2H6O')
                self.assertEqual(preview[0]['review_status'], 'SUPPORTED')
                self.assertEqual(preview[0]['standardization_toolkit'], 'Open Babel')
                self.assertFalse(preview[0]['already_in_structure_database'])
                self.assertEqual(
                    preview[0]['parent_inchikey'],
                    preview[0]['inchikey'],
                )
                self.assertTrue(preview[0]['parent_is_self'])
                evidence = structure_curation.get_evidence_preview(run_id)
                self.assertEqual(len(evidence), 1)
                self.assertEqual(
                    evidence[0]['web_finding_id'],
                    str(finding.pk),
                )
                self.assertEqual(evidence[0]['source_url'], finding.source_url)
                self.assertEqual(evidence[0]['assessment'], 'SUPPORTS')
                state = structure_curation.read_run_state(run_id)
                self.assertEqual(state['structures_supported'], 1)
                self.assertEqual(state['structures_needing_review'], 0)
                self.assertEqual(state['already_in_structure_database_count'], 0)
                self.assertEqual(state['new_supported_structures'], 1)
                self.assertEqual(
                    state['evidence_assessments'],
                    {'SUPPORTS': 1},
                )
                self.assertEqual(state['rejected_findings'], 0)
                self.assertEqual(state['review_flag_counts'], [])


class StructureCurationTriageTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='structure-triage-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.url = reverse('structure_curation')

    def test_conflicting_parsed_inputs_can_be_accepted_as_separate_structures(
        self,
    ):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchi_key = 'VUHQYAFIMXEKOM-UHFFFAOYSA-N'
        smiles_key = 'NNJNGOPGQSGTBR-UHFFFAOYSA-N'
        derived_structures = [
            {
                'source': 'inchi',
                'inchikey': inchi_key,
                'inchi': 'InChI=1S/example-inchi-structure',
                'canonical_smiles': 'NCCN.[Li+].[Li+]',
                'isomeric_smiles': 'NCCN.[Li+].[Li+]',
                'formula': 'C26H19N5O13S4.4Li',
            },
            {
                'source': 'canonical_smiles',
                'inchikey': smiles_key,
                'inchi': 'InChI=1S/example-smiles-structure',
                'canonical_smiles': 'NCCN.[Li+]',
                'isomeric_smiles': 'NCCN.[Li+]',
                'formula': 'C26H19N5O13S4Li',
            },
        ]

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(
                STRUCTURE_CURATION_ROOT=temporary_directory,
            ):
                source = WebSource.objects.create(
                    name='Split structure test provider',
                    provider_code='split_test',
                )
                search_term = SearchTerm.objects.create(
                    term_value='split-structure-test',
                )
                task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=task,
                    raw_payload='{}',
                )
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                structure_rows = []
                parent_rows = []
                evidence_rows = []
                for key, formula in (
                    (inchi_key, 'C26H19N5O13S4.4Li'),
                    (smiles_key, 'C26H19N5O13S4Li'),
                ):
                    row = {
                        'inchikey': key,
                        'inchi': f'InChI=1S/{key}',
                        'canonical_smiles': f'candidate-{key}',
                        'isomeric_smiles': f'candidate-{key}',
                        'molecular_formula': formula,
                        'molecular_weight': None,
                        'exact_mass': None,
                        'standardization_toolkit': 'Open Babel',
                        'standardization_version': 'test',
                        'standardization_policy': 'test',
                        'review_status': 'REVIEW',
                        'review_flags': (
                            ['conflicting_structure_inputs']
                            if key == inchi_key
                            else [
                                'conflicting_structure_inputs',
                                'reported_formula_mismatch',
                                'reported_molecular_weight_mismatch',
                            ]
                        ),
                        'parent_inchikey': inchi_key,
                        'parent_is_self': key == inchi_key,
                        'parent_generation_status': 'GENERATED',
                    }
                    structure_rows.append(row)
                    parent_rows.append(dict(row))
                    evidence_rows.append({
                        'structure_inchikey': key,
                        'web_finding_id': str(finding.pk),
                        'provider_code': 'split_test',
                        'assessment': 'SUPPORTS',
                        'reported_values': {},
                        'validation': [{
                            'key_checks': ['inchi_smiles_disagree'],
                            'derived_structures': derived_structures,
                        }],
                    })
                for filename, records in (
                    ('preview.jsonl', structure_rows),
                    ('parent_structures.jsonl', parent_rows),
                    ('structure_evidence.jsonl', evidence_rows),
                ):
                    (run_dir / filename).write_text(
                        ''.join(
                            json.dumps(record) + '\n'
                            for record in records
                        ),
                        encoding='utf-8',
                    )
                (run_dir / 'invalid.jsonl').write_text(
                    '',
                    encoding='utf-8',
                )

                queue = structure_curation.get_review_queue(run_id)
                self.assertEqual(
                    {
                        row['inchikey']: row['split_structure_input_keys']
                        for group in queue['groups']
                        for row in group['records']
                    },
                    {
                        inchi_key: sorted([inchi_key, smiles_key]),
                        smiles_key: sorted([inchi_key, smiles_key]),
                    },
                )
                page = self.client.get(self.url, {'run_id': run_id})
                self.assertContains(
                    page,
                    'Keep both as separate structures',
                )

                response = self.client.post(self.url, {
                    'action': 'triage_split',
                    'run_id': run_id,
                    'inchikey': inchi_key,
                })

                self.assertEqual(response.status_code, 302)
                self.assertEqual(
                    {
                        row['inchikey']: row['triage_decision']
                        for row in structure_curation.get_preview(run_id)
                    },
                    {
                        inchi_key: 'ACCEPTED',
                        smiles_key: 'ACCEPTED',
                    },
                )

                summary = structure_curation.commit_reviewed_preview(run_id)

                self.assertEqual(summary['accepted_structures'], 2)
                self.assertEqual(
                    set(Structure.objects.values_list('inchikey', flat=True)),
                    {inchi_key, smiles_key},
                )
                self.assertEqual(
                    set(
                        StructureEvidence.objects.filter(
                            web_finding=finding,
                        ).values_list('structure__inchikey', flat=True)
                    ),
                    {inchi_key, smiles_key},
                )

    def test_superseded_batch_cannot_be_committed(self):
        import tempfile
        import uuid
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                batch = StructureCurationBatch.objects.create(
                    run_id=run_id,
                    batch_limit=1,
                    random_seed=0,
                    status=StructureCurationBatch.Status.SUPERSEDED,
                )
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'batch_id': str(batch.pk),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                for filename in (
                    'preview.jsonl',
                    'parent_structures.jsonl',
                    'structure_evidence.jsonl',
                    'invalid.jsonl',
                ):
                    (run_dir / filename).write_text('', encoding='utf-8')

                with self.assertRaisesMessage(
                    ValueError,
                    'This curation batch was superseded',
                ):
                    structure_curation.commit_reviewed_preview(run_id)

    def test_partial_commit_summary_remains_visible_in_preview(self):
        import tempfile
        import uuid
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'database_commit_partial': {
                        'committed_at': '2026-10-30T15:05:33+00:00',
                        'accepted_structures': 279,
                        'new_structure_rows': 320,
                        'new_parent_rows': 161,
                        'evidence_links_processed': 980,
                        'identifiers_linked': 42,
                    },
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                for filename in (
                    'preview.jsonl',
                    'parent_structures.jsonl',
                    'structure_evidence.jsonl',
                    'invalid.jsonl',
                ):
                    (run_dir / filename).write_text('', encoding='utf-8')
                (run_dir / 'triage.json').write_text(
                    '{"groups": {}, "structures": {}}',
                    encoding='utf-8',
                )

                response = self.client.get(self.url, {'run_id': run_id})

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'A previous add completed on')
                self.assertContains(response, '279 accepted structure(s)')
                self.assertContains(response, '320')
                self.assertContains(
                    response,
                    'The button below applies to the accepted structures not yet committed.',
                )

    def test_partial_commit_retries_only_uncommitted_evidence(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        committed_key = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        pending_key = 'QUSNBJAOOMFDIB-UHFFFAOYSA-N'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Partial commit provider',
                    provider_code='partial_commit',
                )
                search_term = SearchTerm.objects.create(
                    term_value='partial commit test',
                )
                task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                committed_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/committed',
                    raw_payload='{}',
                )
                pending_finding = WebFinding.objects.create(
                    search_task=task,
                    source_url='https://example.test/pending',
                    raw_payload='{}',
                )
                committed_structure = Structure.objects.create(
                    inchikey=committed_key,
                    inchi='InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                    canonical_smiles='CCO',
                    isomeric_smiles='CCO',
                    molecular_formula='C2H6O',
                    molecular_weight=46.069,
                    exact_mass=46.0418648,
                )
                StructureEvidence.objects.create(
                    structure=committed_structure,
                    web_finding=committed_finding,
                    assessment='SUPPORTS',
                    reported_values={},
                    validation={},
                )
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'committed_inchikeys': [committed_key],
                    'database_commit_partial': {
                        'accepted_structures': 1,
                        'new_structure_rows': 1,
                        'new_parent_rows': 0,
                        'evidence_links_processed': 1,
                        'identifiers_linked': 0,
                    },
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                rows = [
                    {
                        'inchikey': committed_key,
                        'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                        'canonical_smiles': 'CCO',
                        'isomeric_smiles': 'CCO',
                        'molecular_formula': 'C2H6O',
                        'molecular_weight': 46.069,
                        'exact_mass': 46.0418648,
                        'parent_inchikey': committed_key,
                        'parent_generation_status': 'GENERATED',
                        'review_flags': [],
                    },
                    {
                        'inchikey': pending_key,
                        'inchi': 'InChI=1S/C2H7N/c1-2-3/h2-3H2,1H3',
                        'canonical_smiles': 'CCN',
                        'isomeric_smiles': 'CCN',
                        'molecular_formula': 'C2H7N',
                        'molecular_weight': 45.085,
                        'exact_mass': 45.0578492,
                        'parent_inchikey': pending_key,
                        'parent_generation_status': 'GENERATED',
                        'review_flags': [],
                    },
                ]
                (run_dir / 'preview.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in rows),
                    encoding='utf-8',
                )
                (run_dir / 'parent_structures.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in rows),
                    encoding='utf-8',
                )
                evidence = [
                    {
                        'structure_inchikey': committed_key,
                        'web_finding_id': str(committed_finding.pk),
                        'assessment': 'SUPPORTS',
                        'reported_values': {},
                        'validation': {},
                    },
                    {
                        'structure_inchikey': pending_key,
                        'web_finding_id': str(pending_finding.pk),
                        'assessment': 'SUPPORTS',
                        'reported_values': {},
                        'validation': {},
                    },
                ]
                (run_dir / 'structure_evidence.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in evidence),
                    encoding='utf-8',
                )
                (run_dir / 'invalid.jsonl').write_text('', encoding='utf-8')
                (run_dir / 'triage.json').write_text(
                    '{"groups": {}, "structures": {}}',
                    encoding='utf-8',
                )

                summary = structure_curation.commit_reviewed_preview(run_id)

                self.assertEqual(summary['accepted_structures'], 1)
                self.assertEqual(summary['evidence_links_processed'], 1)
                self.assertTrue(Structure.objects.filter(inchikey=pending_key).exists())
                self.assertEqual(StructureEvidence.objects.count(), 2)
                self.assertTrue(
                    structure_curation.read_run_state(run_id).get(
                        'database_commit'
                    )
                )

    def test_committed_structures_are_removed_from_active_review_queue(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        committed_key = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        remaining_key = 'QUSNBJAOOMFDIB-UHFFFAOYSA-N'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'committed_inchikeys': [committed_key],
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                rows = [
                    {
                        'inchikey': committed_key,
                        'canonical_smiles': 'CCO',
                        'molecular_formula': 'C2H6O',
                        'review_flags': [],
                        'already_in_structure_database': False,
                    },
                    {
                        'inchikey': remaining_key,
                        'canonical_smiles': 'CCN',
                        'molecular_formula': 'C2H7N',
                        'review_flags': [],
                        'already_in_structure_database': False,
                    },
                ]
                (run_dir / 'preview.jsonl').write_text(
                    ''.join(json.dumps(row) + '\n' for row in rows),
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    '',
                    encoding='utf-8',
                )
                (run_dir / 'triage.json').write_text(
                    json.dumps({'groups': {}, 'structures': {}}),
                    encoding='utf-8',
                )

                queue = structure_curation.get_review_queue(run_id)

                self.assertEqual(
                    [row['inchikey'] for row in queue['ready_new']],
                    [remaining_key],
                )

    def test_group_triage_and_individual_smiles_edit_update_exports(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        run_id = str(uuid.uuid4())
        old_key = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        flags = [
            'conflicting_computed_exact_mass',
            'reported_formula_unparseable',
        ]
        group_id = structure_curation._review_group_id(flags)
        structure_row = {
            'inchikey': old_key,
            'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
            'canonical_smiles': 'CCO',
            'isomeric_smiles': 'CCO',
            'molecular_formula': 'C2H6O',
            'molecular_weight': 46.069,
            'exact_mass': 46.0418648,
            'standardization_toolkit': 'Open Babel',
            'standardization_version': '3',
            'standardization_policy': 'test',
            'review_status': 'REVIEW',
            'review_flags': flags,
            'computed_value_variants': {
                'exact_mass': [
                    {
                        'value': 46.0418648,
                        'finding_count': 2,
                        'finding_ids': ['finding-1'],
                        'providers': ['provider_a'],
                    },
                    {
                        'value': 46.0419,
                        'finding_count': 1,
                        'finding_ids': ['finding-2'],
                        'providers': ['provider_b'],
                    },
                ],
                'canonical_smiles': [{
                    'value': 'CCO',
                    'finding_count': 1,
                    'finding_ids': ['finding-1'],
                    'providers': ['provider_a'],
                }],
            },
            'parent_id': None,
            'parent_inchikey': old_key,
            'parent_is_self': True,
            'parent_generation_status': 'GENERATED',
        }
        evidence_row = {
            'structure_inchikey': old_key,
            'web_finding_id': None,
            'provider_code': 'cas_common',
            'assessment': 'SUPPORTS',
            'reported_values': {
                'canonical_smiles': 'CCO',
                'molecularFormula': 'C&lt;sub&gt;2&lt;/sub&gt;H&lt;sub&gt;6&lt;/sub&gt;O',
                'molecularWeight': '46.07',
            },
            'validation': {
                'key_checks': ['structure_key_match'],
                'computed_values_for_candidate': [{
                    'reported_checks': {
                        'exact_mass': 'mismatch',
                        'formula': 'unparseable',
                    },
                }],
            },
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='CAS Common Chemistry curation test',
                    provider_code='cas_common',
                )
                search_term = SearchTerm.objects.create(term_value='triage test')
                search_task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=search_task,
                    source_url='https://example.test/structure',
                    raw_payload='{"smile":"CCO"}',
                )
                evidence_row['web_finding_id'] = str(finding.pk)
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                    'review_groups': [{
                        'id': group_id,
                        'flags': flags,
                        'complexity': 'SIMPLE',
                        'count': 1,
                        'sample_inchikeys': [old_key],
                    }],
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                (run_dir / 'preview.jsonl').write_text(
                    json.dumps(structure_row) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'parent_structures.jsonl').write_text(
                    json.dumps(structure_row) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'structure_evidence.jsonl').write_text(
                    json.dumps(evidence_row) + '\n',
                    encoding='utf-8',
                )
                (run_dir / 'invalid.jsonl').write_text('', encoding='utf-8')

                page = self.client.get(self.url, {'run_id': run_id})
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'Review queue by issue pattern')
                self.assertContains(page, 'Good structures — no review required')
                self.assertContains(page, 'Add reviewed structures to the database')
                review_record = structure_curation.get_review_queue(run_id)[
                    'groups'
                ][0]['records'][0]
                self.assertEqual(
                    review_record['issue_edit_fields'],
                    ['exact_mass', 'molecular_formula'],
                )
                self.assertEqual(
                    [issue['field'] for issue in review_record['issue_details']],
                    ['exact_mass', 'molecular_formula'],
                )
                self.assertEqual(
                    [
                        variant['value']
                        for variant in review_record['issue_details'][0]['variants']
                    ],
                    [46.0418648, 46.0419],
                )
                formula_evidence = review_record['issue_details'][1]['evidence']
                self.assertEqual(
                    formula_evidence[0]['reported_values'][0]['field'],
                    'molecularFormula',
                )

                group_response = self.client.post(self.url, {
                    'action': 'triage_group',
                    'run_id': run_id,
                    'group_id': group_id,
                    'decision': 'ACCEPTED',
                })
                self.assertEqual(group_response.status_code, 302)
                self.assertEqual(
                    structure_curation.get_preview(run_id)[0][
                        'triage_decision'
                    ],
                    'ACCEPTED',
                )

                edit_response = self.client.post(self.url, {
                    'action': 'triage_structure',
                    'run_id': run_id,
                    'inchikey': old_key,
                    'decision': 'ACCEPTED',
                    'edited_smiles': 'CCN.CC(=O)O',
                    'molecular_formula': 'C4H11NO2',
                    'exact_mass': '105.078979',
                })
                self.assertEqual(edit_response.status_code, 302)
                edited = structure_curation.get_preview(run_id)[0]
                self.assertNotEqual(edited['inchikey'], old_key)
                self.assertEqual(
                    edited['canonical_smiles'],
                    'CC(=O)O.CCN',
                )
                self.assertEqual(edited['molecular_formula'], 'C4H11NO2')
                self.assertEqual(edited['exact_mass'], 105.078979)
                self.assertEqual(edited['triage_decision'], 'ACCEPTED')
                self.assertEqual(
                    structure_curation.get_review_queue(run_id)[
                        'needs_review_count'
                    ],
                    0,
                )
                parent = structure_curation.get_parent_preview(run_id)[0]
                self.assertNotEqual(parent['inchikey'], edited['inchikey'])
                self.assertEqual(parent['molecular_formula'], 'C2H4O2')
                self.assertEqual(
                    structure_curation.get_evidence_preview(run_id)[0][
                        'structure_inchikey'
                    ],
                    edited['inchikey'],
                )
                self.assertEqual(Structure.objects.count(), 0)
                commit_response = self.client.post(self.url, {
                    'action': 'commit_preview',
                    'run_id': run_id,
                })
                self.assertEqual(commit_response.status_code, 302)
                self.assertEqual(
                    structure_curation.read_run_state(run_id)['commit_job'][
                        'status'
                    ],
                    'QUEUED',
                )
                structure_curation.run_reviewed_preview_commit(run_id)
                summary = structure_curation.read_run_state(run_id)[
                    'database_commit'
                ]
                committed = Structure.objects.get(inchikey=edited['inchikey'])
                self.assertEqual(summary['accepted_structures'], 1)
                self.assertNotEqual(committed.parent_id, committed.pk)
                self.assertEqual(committed.molecular_formula, 'C4H11NO2')
                self.assertEqual(committed.exact_mass, 105.078979)
                component_links = list(
                    StructureComponent.objects.filter(
                        structure=committed,
                    ).select_related('component')
                )
                self.assertEqual(len(component_links), 2)
                parent_link = next(
                    link for link in component_links
                    if link.is_parent_component
                )
                self.assertEqual(parent_link.component.molecular_formula, 'C2H4O2')
                self.assertEqual(parent_link.component_id, committed.parent_id)
                self.assertEqual(StructureEvidence.objects.count(), 1)
                self.assertEqual(
                    StructureEvidence.objects.get().structure_id,
                    committed.pk,
                )
                self.assertTrue(
                    structure_curation.read_run_state(run_id)['database_commit']
                )

    def test_random_finding_batches_leave_unselected_findings_pending(self):
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Batch provider',
                    provider_code='cas_common',
                )
                search_term = SearchTerm.objects.create(term_value='batch test')
                search_task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                for index in range(5):
                    WebFinding.objects.create(
                        search_task=search_task,
                        source_url=f'https://example.test/{index}',
                        raw_payload='{}',
                    )

                first_run_id = str(uuid.uuid4())
                structure_curation.create_run(first_run_id)
                first_batch = structure_curation.prepare_finding_batch(
                    first_run_id,
                    batch_limit=2,
                )
                first_ids = set(
                    first_batch.findings.values_list('finding_id', flat=True)
                )
                first_state = structure_curation.read_run_state(first_run_id)
                self.assertEqual(first_batch.candidate_count, 5)
                self.assertEqual(first_batch.selected_count, 2)
                self.assertEqual(
                    structure_curation._findings_queryset(first_state).count(),
                    2,
                )
                first_page = list(
                    structure_curation._findings_queryset(first_state)
                )
                first_state['cursor_updated_at'] = (
                    first_page[0].updated_at.isoformat()
                )
                first_state['cursor_id'] = str(first_page[0].pk)
                self.assertEqual(
                    list(
                        structure_curation._findings_queryset(first_state)
                    ),
                    first_page[1:],
                )

                first_state['status'] = 'COMPLETED'
                structure_curation._write_state(first_run_id, first_state)
                second_run_id = str(uuid.uuid4())
                structure_curation.create_run(second_run_id)
                second_batch = structure_curation.prepare_finding_batch(
                    second_run_id,
                    batch_limit=2,
                )
                second_ids = set(
                    second_batch.findings.values_list('finding_id', flat=True)
                )
                self.assertEqual(second_batch.candidate_count, 3)
                self.assertEqual(second_batch.selected_count, 2)
                self.assertFalse(first_ids.intersection(second_ids))
                self.assertEqual(
                    StructureCurationBatchFinding.objects.filter(
                        batch__isnull=True,
                        status=StructureCurationBatchFinding.Status.PENDING,
                    ).count(),
                    1,
                )
                self.assertEqual(
                    StructureCurationBatch.objects.count(),
                    2,
                )
                structure_curation.mark_run_failed(
                    second_run_id,
                    'simulated worker failure',
                )
                self.assertEqual(
                    StructureCurationBatch.objects.get(
                        pk=second_batch.pk,
                    ).status,
                    StructureCurationBatch.Status.FAILED,
                )
                self.assertEqual(
                    StructureCurationBatchFinding.objects.filter(
                        batch__isnull=True,
                        status=StructureCurationBatchFinding.Status.PENDING,
                    ).count(),
                    3,
                )

    def test_batch_with_only_rejected_findings_can_be_finalized(self):
        import tempfile
        import uuid

        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Rejected batch provider',
                    provider_code='rejected_batch',
                )
                search_term = SearchTerm.objects.create(term_value='rejected')
                search_task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=search_task,
                    raw_payload='{}',
                )
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                batch = StructureCurationBatch.objects.create(
                    run_id=run_id,
                    batch_limit=1,
                    random_seed=1,
                    candidate_count=1,
                    selected_count=1,
                    status=StructureCurationBatch.Status.READY,
                )
                StructureCurationBatchFinding.objects.create(
                    batch=batch,
                    finding=finding,
                    status=StructureCurationBatchFinding.Status.REJECTED,
                )
                state.update({
                    'status': 'COMPLETED',
                    'batch_id': str(batch.pk),
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                for path in (
                    structure_curation.get_report_path(run_id),
                    structure_curation.get_parent_report_path(run_id),
                    structure_curation.get_evidence_report_path(run_id),
                    structure_curation.get_invalid_report_path(run_id),
                ):
                    path.touch()

                summary = structure_curation.commit_reviewed_preview(run_id)

                batch.refresh_from_db()
                self.assertEqual(summary['accepted_structures'], 0)
                self.assertEqual(batch.status, StructureCurationBatch.Status.COMMITTED)
                self.assertEqual(
                    structure_curation.read_run_state(run_id)['database_commit'],
                    summary,
                )
                with self.assertRaisesMessage(ValueError, 'Saved WebFinding batches'):
                    structure_curation.delete_run(run_id)

    def test_accepting_new_evidence_does_not_overwrite_existing_chemistry(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchikey = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Existing chemistry provider',
                    provider_code='existing_chemistry',
                )
                search_term = SearchTerm.objects.create(
                    term_value='64-17-5',
                    term_type='CAS',
                )
                search_task = SearchTask.objects.create(
                    search_term=search_term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=search_task,
                    raw_payload='{}',
                )
                existing = Structure.objects.create(
                    inchikey=inchikey,
                    canonical_smiles='CCO',
                    isomeric_smiles='CCO',
                    molecular_formula='C2H6O',
                    molecular_weight=46.069,
                    exact_mass=46.0418648,
                )
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                row = {
                    'inchikey': inchikey,
                    'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                    'canonical_smiles': 'CCO',
                    'isomeric_smiles': 'CCO',
                    'molecular_formula': 'C2H6O',
                    'molecular_weight': 46.069,
                    'exact_mass': 47.0,
                    'review_status': 'REVIEW',
                    'review_flags': ['conflicting_existing_exact_mass'],
                    'parent_id': None,
                    'parent_inchikey': inchikey,
                    'parent_is_self': True,
                    'parent_generation_status': 'GENERATED',
                }
                evidence = {
                    'structure_inchikey': inchikey,
                    'web_finding_id': str(finding.pk),
                    'provider_code': 'existing_chemistry',
                    'assessment': 'SUPPORTS',
                    'reported_values': {
                        'preferred_name': '(11β)-test compound',
                    },
                    'validation': {
                        'provider_note': 'α/β stereochemistry labels',
                    },
                }
                for filename, records in (
                    ('preview.jsonl', [row]),
                    ('parent_structures.jsonl', [row]),
                    ('structure_evidence.jsonl', [evidence]),
                ):
                    (run_dir / filename).write_text(
                        ''.join(json.dumps(record) + '\n' for record in records),
                        encoding='utf-8',
                    )
                (run_dir / 'invalid.jsonl').write_text('', encoding='utf-8')

                structure_curation.save_structure_decision(
                    run_id,
                    inchikey,
                    'ACCEPTED',
                )
                summary = structure_curation.commit_reviewed_preview(run_id)

                existing.refresh_from_db()
                self.assertEqual(existing.exact_mass, 46.0418648)
                self.assertEqual(existing.molecular_formula, 'C2H6O')
                self.assertEqual(summary['accepted_structures'], 1)
                self.assertEqual(summary['identifiers_linked'], 1)
                self.assertEqual(StructureEvidence.objects.count(), 1)
                self.assertTrue(
                    Identifier.objects.filter(
                        identifier_type=Identifier.IdentifierType.CAS,
                        identifier_value='64-17-5',
                        structures=existing,
                    ).exists()
                )
                self.assertEqual(
                    StructureEvidence.objects.get().structure_id,
                    existing.pk,
                )
                saved_evidence = StructureEvidence.objects.get()
                self.assertEqual(
                    saved_evidence.reported_values['preferred_name'],
                    '(11β)-test compound',
                )
                self.assertEqual(
                    saved_evidence.validation['provider_note'],
                    'α/β stereochemistry labels',
                )

    def test_matching_structure_evidence_fills_key_only_placeholder(self):
        import json
        import tempfile
        import uuid
        from pathlib import Path

        inchikey = 'LFQSCWFLJHTTHZ-UHFFFAOYSA-N'
        with tempfile.TemporaryDirectory() as temporary_directory:
            with override_settings(STRUCTURE_CURATION_ROOT=temporary_directory):
                source = WebSource.objects.create(
                    name='Placeholder enrichment provider',
                    provider_code='placeholder_enrichment',
                )
                term = SearchTerm.objects.create(term_value='ethanol')
                task = SearchTask.objects.create(
                    search_term=term,
                    web_source=source,
                    target_representation='detail',
                )
                finding = WebFinding.objects.create(
                    search_task=task,
                    raw_payload='{}',
                )
                placeholder = Structure.objects.create(inchikey=inchikey)
                run_id = str(uuid.uuid4())
                state = structure_curation.create_run(run_id)
                state.update({
                    'status': 'COMPLETED',
                    'report_format_version': (
                        structure_curation.REPORT_FORMAT_VERSION
                    ),
                })
                structure_curation._write_state(run_id, state)
                run_dir = Path(temporary_directory) / run_id
                row = {
                    'inchikey': inchikey,
                    'inchi': 'InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3',
                    'canonical_smiles': 'CCO',
                    'isomeric_smiles': 'CCO',
                    'molecular_formula': 'C2H6O',
                    'molecular_weight': 46.069,
                    'exact_mass': 46.0418648,
                    'standardization_toolkit': 'RDKit',
                    'standardization_version': 'test',
                    'standardization_policy': 'test',
                    'review_status': 'SUPPORTED',
                    'review_flags': [],
                    'parent_id': None,
                    'parent_inchikey': inchikey,
                    'parent_is_self': True,
                    'parent_generation_status': 'GENERATED',
                    'components': [],
                }
                evidence = {
                    'structure_inchikey': inchikey,
                    'web_finding_id': str(finding.pk),
                    'provider_code': 'placeholder_enrichment',
                    'assessment': 'SUPPORTS',
                    'reported_values': {},
                    'validation': {},
                }
                for filename, records in (
                    ('preview.jsonl', [row]),
                    ('parent_structures.jsonl', [row]),
                    ('structure_evidence.jsonl', [evidence]),
                ):
                    (run_dir / filename).write_text(
                        ''.join(
                            json.dumps(record) + '\n'
                            for record in records
                        ),
                        encoding='utf-8',
                    )
                (run_dir / 'invalid.jsonl').write_text(
                    '',
                    encoding='utf-8',
                )

                structure_curation.commit_reviewed_preview(run_id)

                placeholder.refresh_from_db()
                self.assertEqual(placeholder.canonical_smiles, 'CCO')
                self.assertEqual(placeholder.molecular_formula, 'C2H6O')
                self.assertEqual(placeholder.molecular_weight, 46.069)
                self.assertEqual(placeholder.exact_mass, 46.0418648)
                self.assertEqual(StructureEvidence.objects.count(), 1)


class DataGatheringViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username='data-gathering-admin',
            password='test-password',
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.provider = WebSource.objects.create(
            name='PubChem',
            provider_code='pubchem',
            is_active=True,
        )
        self.url = reverse('data_gathering')

    def test_run_action_schedules_provider_and_status_page_shows_it(self):
        response = self.client.post(
            self.url,
            {
                'action': 'run',
                'provider_filter': 'pubchem',
                'batch_size': '25',
                'max_runtime_minutes': '90',
                'max_tasks': '10',
            },
        )

        self.assertRedirects(response, self.url)
        run = DataGatheringRun.objects.get(provider_code='pubchem')
        self.assertEqual(run.status, DataGatheringRun.Status.SCHEDULED)
        self.assertEqual(run.batch_size, 25)
        self.assertEqual(run.max_runtime_minutes, 90)

        page_response = self.client.get(self.url)
        self.assertContains(page_response, 'Data Gathering')
        self.assertContains(page_response, 'Queue selected jobs')

        status_response = self.client.get(reverse('data_gathering_status'))
        self.assertContains(status_response, 'PUBCHEM')
        self.assertContains(status_response, 'Scheduled for')
        self.assertContains(status_response, 'data-has-active-work="true"')
        self.assertEqual(run.max_tasks, 10)
        self.assertFalse(run.force_chemspider_small_batches)
        self.assertContains(page_response, 'name="max_tasks"')

    def test_dashboard_distinguishes_retryable_exhausted_and_skipped_tasks(self):
        retryable_term = SearchTerm.objects.create(
            term_value='retryable-term',
            term_type='NAME',
        )
        exhausted_term = SearchTerm.objects.create(
            term_value='exhausted-term',
            term_type='NAME',
        )
        skipped_term = SearchTerm.objects.create(
            term_value='skipped-term',
            term_type='Masked ID',
        )
        SearchTask.objects.create(
            search_term=retryable_term,
            web_source=self.provider,
            status=SearchTask.TaskStatus.ERROR,
            run_count=1,
        )
        SearchTask.objects.create(
            search_term=exhausted_term,
            web_source=self.provider,
            status=SearchTask.TaskStatus.ERROR,
            run_count=3,
        )
        SearchTask.objects.create(
            search_term=skipped_term,
            web_source=self.provider,
            status=SearchTask.TaskStatus.SKIPPED,
            is_completed=True,
        )

        response = self.client.get(reverse('data_gathering_status'))

        self.assertContains(response, 'Errors to Retry')
        self.assertContains(response, 'Retries Exhausted')
        self.assertContains(response, 'Skipped')
        self.assertContains(response, 'Errors to retry')
        self.assertContains(response, 'Retries exhausted')

    def test_chemspider_small_batch_override_is_saved_for_run(self):
        chemspider = WebSource.objects.create(
            name='ChemSpider',
            provider_code='chemspider',
            is_active=True,
        )

        response = self.client.post(
            self.url,
            {
                'action': 'run',
                'provider_filter': 'chemspider',
                'batch_size': '10',
                'max_runtime_minutes': '90',
                'max_tasks': '10',
                'force_chemspider_small_batches': 'on',
            },
        )

        self.assertRedirects(response, self.url)
        run = DataGatheringRun.objects.get(provider_code=chemspider.provider_code)
        self.assertTrue(run.force_chemspider_small_batches)

    def test_pause_and_resume_actions_update_provider_run(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            batch_size=25,
        )

        pause_response = self.client.post(
            self.url,
            {'action': 'pause_run', 'run_id': str(run.pk)},
        )
        self.assertRedirects(pause_response, self.url)
        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.PAUSED)

        resume_response = self.client.post(
            self.url,
            {'action': 'resume_run', 'run_id': str(run.pk)},
        )
        self.assertRedirects(resume_response, self.url)
        run.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.SCHEDULED)
        self.assertIsNone(run.dispatched_at)

    def test_cancel_action_requeues_processing_tasks_and_records_reason(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            started_at=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
            batch_size=25,
        )
        term = SearchTerm.objects.create(term_value='stuck-task', term_type='NAME')
        task = SearchTask.objects.create(
            search_term=term,
            web_source=self.provider,
            status=SearchTask.TaskStatus.PROCESSING,
            is_completed=False,
            error_message='previous failure',
        )
        task_log = ProviderTaskLog.objects.create(
            provider='pubchem',
            status='RUNNING',
            total_items=1,
        )

        response = self.client.post(
            self.url,
            {
                'action': 'cancel_run',
                'run_id': str(run.pk),
                'cancel_reason': 'PubChem detail batch is failing.',
            },
        )

        self.assertRedirects(response, self.url)
        run.refresh_from_db()
        task.refresh_from_db()
        task_log.refresh_from_db()
        self.assertEqual(run.status, DataGatheringRun.Status.CANCELLED)
        self.assertIn('PubChem detail batch is failing.', run.pause_reason)
        self.assertEqual(task.status, SearchTask.TaskStatus.PENDING)
        self.assertFalse(task.is_completed)
        self.assertFalse(task.error_message)
        self.assertEqual(task_log.status, 'CANCELLED')
        self.assertContains(
            self.client.get(reverse('data_gathering_status')),
            'Failed or Cancelled Runs',
        )
        self.assertContains(
            self.client.get(reverse('data_gathering_status')),
            'Dismiss',
        )

    def test_cancel_action_requires_a_reason(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            status=DataGatheringRun.Status.RUNNING,
        )

        response = self.client.post(
            self.url,
            {'action': 'cancel_run', 'run_id': str(run.pk), 'cancel_reason': '  '},
            follow=True,
        )

        run.refresh_from_db()
        self.assertContains(response, 'Enter a reason for cancelling this run.')
        self.assertEqual(run.status, DataGatheringRun.Status.RUNNING)

    def test_dismiss_action_hides_terminal_run_without_deleting_history(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now(),
            status=DataGatheringRun.Status.CANCELLED,
            pause_reason='Cancelled by operator: no longer needed.',
        )

        before_dismiss = self.client.get(reverse('data_gathering_status'))
        self.assertContains(before_dismiss, 'no longer needed.')

        response = self.client.post(
            self.url,
            {'action': 'dismiss_run', 'run_id': str(run.pk)},
        )

        self.assertRedirects(response, self.url)
        run.refresh_from_db()
        self.assertTrue(run.is_dismissed)
        self.assertEqual(run.status, DataGatheringRun.Status.CANCELLED)
        self.assertTrue(DataGatheringRun.objects.filter(pk=run.pk).exists())
        after_dismiss = self.client.get(reverse('data_gathering_status'))
        self.assertNotContains(after_dismiss, 'no longer needed.')
        self.assertNotContains(after_dismiss, 'Failed or Cancelled Runs')

    def test_run_action_accepts_future_local_schedule(self):
        scheduled_local = timezone.localtime(timezone.now()) + timedelta(hours=8)
        scheduled_input = scheduled_local.strftime('%Y-%m-%dT%H:%M')

        response = self.client.post(
            self.url,
            {
                'action': 'run',
                'provider_filter': 'pubchem',
                'batch_size': '25',
                'max_runtime_minutes': '90',
                'scheduled_for': scheduled_input,
            },
        )

        self.assertRedirects(response, self.url)
        run = DataGatheringRun.objects.get(provider_code='pubchem')
        expected = scheduled_local.replace(second=0, microsecond=0)
        self.assertEqual(run.scheduled_for, expected)

        status_response = self.client.get(reverse('data_gathering_status'))
        self.assertContains(status_response, 'Scheduled for')
        self.assertContains(status_response, 'Recent Activity')
        self.assertNotContains(status_response, 'Recent Runs')

    def test_run_action_rejects_past_schedule(self):
        past_local = timezone.localtime(timezone.now()) - timedelta(hours=1)
        response = self.client.post(
            self.url,
            {
                'action': 'run',
                'provider_filter': 'pubchem',
                'batch_size': '25',
                'max_runtime_minutes': '90',
                'scheduled_for': past_local.strftime('%Y-%m-%dT%H:%M'),
            },
            follow=True,
        )

        self.assertContains(response, 'Scheduled time must be in the future.')
        self.assertFalse(DataGatheringRun.objects.exists())

    def test_undispatched_scheduled_run_can_be_deleted(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now() + timedelta(days=1),
            batch_size=25,
        )

        response = self.client.post(
            self.url,
            {'action': 'delete_scheduled_run', 'run_id': str(run.pk)},
        )

        self.assertRedirects(response, self.url)
        self.assertFalse(DataGatheringRun.objects.filter(pk=run.pk).exists())

    def test_dispatched_scheduled_run_cannot_be_deleted(self):
        run = DataGatheringRun.objects.create(
            provider_code='pubchem',
            scheduled_for=timezone.now() + timedelta(days=1),
            batch_size=25,
            dispatched_at=timezone.now(),
        )

        response = self.client.post(
            self.url,
            {'action': 'delete_scheduled_run', 'run_id': str(run.pk)},
            follow=True,
        )

        self.assertContains(
            response,
            'Only scheduled runs that have not been dispatched can be deleted.',
        )
        self.assertTrue(DataGatheringRun.objects.filter(pk=run.pk).exists())
