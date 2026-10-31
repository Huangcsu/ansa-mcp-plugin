import unittest

from ansa_mcp_bridge.operations.solid_overlap_batches import plan_native_overlap_batches


class NativeOverlapBatchTests(unittest.TestCase):
    def test_complete_graph_is_covered_by_disjoint_bounded_groups(self):
        pairs=[{"kind":"pair","element_ids":[a,b]} for a in range(1,14) for b in range(a+1,14)]
        result=plan_native_overlap_batches(pairs,max_group_size=3)
        self.assertEqual(result["status"],"pass")
        actual=set()
        for batch in result["batches"]:
            first,second=set(batch["first_ids"]),set(batch["second_ids"])
            self.assertFalse(first & second)
            self.assertLessEqual(max(len(first),len(second)),3)
            for a,b in batch["covered_pairs"]:
                self.assertTrue((a in first and b in second) or (a in second and b in first))
                self.assertNotIn((a,b),actual)
                actual.add((a,b))
        self.assertEqual(actual,{tuple(r["element_ids"]) for r in pairs})
        self.assertEqual(result,plan_native_overlap_batches(list(reversed(pairs)),max_group_size=3))

    def test_budget_and_element_unknowns_do_not_claim_complete(self):
        rows=[{"element_ids":[1,2]},{"kind":"element","element_id":3}]
        result=plan_native_overlap_batches(rows,max_batches=0)
        self.assertEqual(result["status"],"incomplete")
        self.assertEqual(result["remaining_pairs"],[[1,2]])
        self.assertEqual(result["unresolved_element_ids"],[3])

    def test_duplicate_candidates_are_deduplicated_and_invalid_ids_rejected(self):
        result=plan_native_overlap_batches([{"element_ids":[1,2]},{"element_ids":[2,1]}])
        self.assertEqual(result["candidate_pair_count"],1)
        for ids in ([1,1],[1,True],[0,2],[1]):
            with self.assertRaises(ValueError): plan_native_overlap_batches([{"element_ids":ids}])


if __name__=="__main__": unittest.main()
