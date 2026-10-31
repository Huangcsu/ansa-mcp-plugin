import copy
import unittest

from ansa_mcp_bridge.operations.cad_sweep_geometry import verify_planar_quad
from test_geometry_features_iges import _build_iges, _entity


CORNERS=[[0.,0.,0.],[4.,0.,0.],[4.,6.,0.],[0.,6.,0.]]
FULL_SQUARE=[(0.,0.),(1.,0.),(1.,1.),(0.,1.)]


def _map_uv(net, point):
    u,v=point[:2]
    weights=((1-u)*(1-v),u*(1-v),(1-u)*v,u*v)
    return [sum(weights[n]*net[n][axis] for n in range(4)) for axis in range(3)]


def quad_entities(uv_vertices=None, net=None):
    if net is None:net=[CORNERS[i] for i in (0,1,3,2)]
    if uv_vertices is None:uv_vertices=FULL_SQUARE
    uv=[[u,v,0.] for u,v in uv_vertices]
    surface=_entity(128,[1,1,1,1,0,0,1,0,0,0,0,1,1,0,0,1,1,1,1,1,1,
                         *[x for p in net for x in p],0,1,0,1])
    curves=[]
    for i,start in enumerate(uv):
        end=uv[(i+1)%4];delta=[b-a for a,b in zip(start,end)]
        coefficients=[x for a,b in zip(start,delta) for x in (a,b,0,0)]
        terminal=[x for a,b in zip(end,delta) for x in (a,b,0,0)]
        curves.append(_entity(112,[3,0,3,1,0,1,*coefficients,*terminal]))
    xyz=[]
    for i,start in enumerate(uv):
        xyz.append(_entity(126,[1,1,0,0,1,0,0,0,1,1,1,1,
                                *_map_uv(net,start),*_map_uv(net,uv[(i+1)%4]),0,1,0,0,0]))
    return [surface,*curves,_entity(102,[4,3,5,7,9]),*xyz,
            _entity(102,[4,13,15,17,19]),_entity(142,[0,1,11,21,1]),_entity(144,[1,1,0,23])]


def native_corners(net, uv_vertices):
    return [_map_uv(net,point) for point in uv_vertices]


class CadSweepGeometryTests(unittest.TestCase):
    def test_full_finite_patch_and_both_trim_representations_are_verified(self):
        result=verify_planar_quad(_build_iges(quad_entities()),CORNERS)
        self.assertTrue(result['continuous_export_domain_verified'])
        self.assertFalse(result['exact_native_CAD_equivalence_proven'])
        self.assertEqual(result['representation'],'IGES128_bilinear_full_square_trim')
        self.assertEqual(result['maximum_corner_or_trim_error_mm'],0.)
        self.assertEqual(result['maximum_control_plane_error_mm'],0.)
        self.assertEqual(result['parameter_curve_terminal_metadata'],['consistent']*4)
        self.assertEqual(result['tolerance_mm'],1e-7)
        self.assertTrue(verify_planar_quad(_build_iges(quad_entities()),list(reversed(CORNERS)))['continuous_export_domain_verified'])

    def test_full_square_keeps_nonaffine_bilinear_surface_support(self):
        net=[[0.,0.,0.],[4.,0.,0.],[0.,6.,0.],[5.,7.,0.]]
        result=verify_planar_quad(_build_iges(quad_entities(net=net)),native_corners(net,FULL_SQUARE))
        self.assertTrue(result['continuous_export_domain_verified'])
        self.assertEqual(result['representation'],'IGES128_bilinear_full_square_trim')

    def test_local_rectangle_and_oblique_convex_trim_are_verified(self):
        net=[[0.,0.,0.],[10.,0.,0.],[0.,10.,0.],[10.,10.,0.]]
        trims=[[(.1,.2),(.8,.2),(.8,.7),(.1,.7)],
               [(.2,.1),(.9,.3),(.7,.85),(.1,.65)]]
        for uv_vertices in trims:
            result=verify_planar_quad(_build_iges(quad_entities(uv_vertices,net)),
                                      native_corners(net,uv_vertices))
            self.assertTrue(result['continuous_export_domain_verified'])
            self.assertEqual(result['representation'],'IGES128_affine_planar_local_convex_quad_trim')
            self.assertEqual(len(result['parameter_curve_terminal_metadata']),4)
            self.assertFalse(result['exact_native_CAD_equivalence_proven'])

    def test_local_trim_outside_domain_self_crossing_wrong_curve_and_nonaffine_surface_are_rejected(self):
        net=[[0.,0.,0.],[10.,0.,0.],[0.,10.,0.],[10.,10.,0.]]
        outside=[(-.1,.2),(.8,.2),(.8,.7),(-.1,.7)]
        bow_tie=[(.1,.1),(.9,.9),(.1,.9),(.9,.1)]
        for uv_vertices in (outside,bow_tie):
            self.assertEqual(verify_planar_quad(_build_iges(quad_entities(uv_vertices,net)),
                                                native_corners(net,uv_vertices))['status'],'unsupported')
        local=[(.1,.2),(.8,.2),(.8,.7),(.1,.7)]
        entities=quad_entities(local,net)
        entities[6]['parameters'][13]=str(float(entities[6]['parameters'][13])+.01)
        self.assertEqual(verify_planar_quad(_build_iges(entities),native_corners(net,local))['status'],'unsupported')
        for cross_term in (1.,1e-8):
            nonaffine=[[0.,0.,0.],[10.,0.,0.],[0.,10.,0.],[10.+cross_term,10.,0.]]
            self.assertEqual(verify_planar_quad(_build_iges(quad_entities(local,nonaffine)),
                                                native_corners(nonaffine,local))['status'],'unsupported')

    def test_float_scale_parameter_noise_keeps_the_full_segment_tolerance(self):
        net=[[0.,0.,0.],[10.,0.,0.],[0.,10.,0.],[10.,10.,0.]]
        local=[(.25,.25),(.75,.25),(.75,.75),(.25,.75)]
        entities=quad_entities(local,net)
        entities[1]['parameters'][13]='1e-15'
        entities[1]['parameters'][14]='-1e-15'
        raw=_build_iges(entities);corners=native_corners(net,local)
        result=verify_planar_quad(raw,corners)
        self.assertTrue(result['continuous_export_domain_verified'])
        self.assertGreater(result['parameter_curve_mapped_deviation_bound_mm'][0],0.)
        self.assertEqual(len(result['parameter_curve_terminal_metadata']),4)
        self.assertEqual(verify_planar_quad(raw,corners,tolerance=1e-16)['status'],'unsupported')

    def test_planar_native_corners_do_not_accept_bowed_or_higher_order_patch(self):
        entities=quad_entities();entities[0]['parameters'][24]='0.1'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')
        entities=quad_entities();entities[0]['parameters'][3]='2'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')

    def test_inner_hole_and_non_square_preferred_uv_trim_are_rejected(self):
        entities=quad_entities();entities[-1]['parameters'][3]='1'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')
        entities=quad_entities();entities[1]['parameters'][8]='0.5'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')

    def test_curved_parameter_trim_is_rejected(self):
        entities=quad_entities();entities[1]['parameters'][9]='1e-8'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')

    def test_inconsistent_space_curve_and_extra_geometry_are_rejected(self):
        entities=quad_entities();entities[6]['parameters'][13]='0.25'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')
        entities=quad_entities();entities.append(_entity(110,[0,0,0,1,0,0]))
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')

    def test_unknown_link_and_nonfinite_public_inputs_fail_closed(self):
        entities=quad_entities();entities[-2]['parameters'][2]='999'
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')
        entities=quad_entities();entities[-1]['form']=1
        self.assertEqual(verify_planar_quad(_build_iges(entities),CORNERS)['status'],'unsupported')
        for tolerance in (True,float('nan'),0.):
            with self.assertRaises(ValueError):verify_planar_quad(_build_iges(quad_entities()),CORNERS,tolerance)
        with self.assertRaises(ValueError):verify_planar_quad(b'broken',CORNERS)


if __name__=='__main__':unittest.main()
