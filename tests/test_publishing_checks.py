import unittest
from workbench.browser import NeedsAttention
from workbench.publishing_checks import declaration_state,check_publish_declarations


class PublishingDeclarationTests(unittest.TestCase):
    def test_native_and_rendered_state_must_agree(self):
        self.assertTrue(declaration_state(True,'byte-checkbox byte-checkbox-checked'))
        self.assertFalse(declaration_state(False,'byte-checkbox checkbot-item'))
        for native,classes in [(False,'byte-checkbox-checked'),(True,'byte-checkbox')]:
            with self.assertRaises(NeedsAttention): declaration_state(native,classes)

    def test_only_exact_selected_class_is_recognized(self):
        self.assertFalse(declaration_state(False,'byte-checkbox byte-checkbox-checked-other'))

    def test_first_release_must_be_off_and_ai_on(self):
        check_publish_declarations({'checked':False,'classes':'byte-checkbox'},{'checked':True,'classes':'byte-checkbox byte-checkbox-checked'})
        with self.assertRaises(NeedsAttention):
            check_publish_declarations({'checked':True,'classes':'byte-checkbox-checked'},{'checked':True,'classes':'byte-checkbox-checked'})
        with self.assertRaises(NeedsAttention):
            check_publish_declarations({'checked':False,'classes':'byte-checkbox'},{'checked':False,'classes':'byte-checkbox'})

    def test_missing_visible_control_is_not_an_unchecked_declaration(self):
        for classes in ['',None,'unrelated-control']:
            with self.assertRaises(NeedsAttention): declaration_state(False,classes)
