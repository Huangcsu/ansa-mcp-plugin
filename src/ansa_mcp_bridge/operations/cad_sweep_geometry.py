"""Finite planar FACE proof over native IGES exports, never a spline fitter.

Only an untransformed 2x2 degree-one, unit-weight surface trimmed by either the
full unit square or a local convex quadrilateral is supported. Local trims also
require an affine surface. Both parameter and model-space curves are read. The
proof is bounded by the representation/precision of SaveFileAsIGES.
"""
import copy
import math

from . import geometry_features_iges as iges


def _require(condition, message):
    if not condition:
        raise iges._Unsupported(message)


def _params(entity, size):
    _require(len(entity.parameters) in (size, size+2), 'unsupported IGES parameter count')
    iges._require_zero_reference_tail(entity.parameters, size, str(entity.entity_type))
    return entity.parameters


def _integers(values):
    return tuple(iges._nonnegative_integer(v, 'IGES integer') for v in values)


def _numbers(values):
    return tuple(iges._finite_number(v, 'IGES coordinate') for v in values)


def _type112_export_line(entity):
    """Read a type 112 line and bound only float-scale higher-order noise."""
    try:
        line=iges._type112_line(entity)
        return line,[0.,0.,0.]
    except iges._Unsupported as exc:
        if 'polynomial is not strictly linear' not in str(exc):
            raise
    p=_params(entity,31)
    ctype,continuity,dimension,segments=_integers(p[1:5])
    _require(ctype==3,'type 112 supports only CTYPE 3 cubic splines')
    _require(continuity in (0,1,2),'type 112 continuity must be 0, 1, or 2')
    _require(dimension==3 and segments==1,'type 112 supports one three-dimensional polynomial segment')
    start_parameter,end_parameter=_numbers(p[5:7]);interval=end_parameter-start_parameter
    _require(math.isfinite(interval) and interval>0.,'type 112 breakpoints must define a positive finite interval')
    coefficients=_numbers(p[7:19]);terminal=_numbers(p[19:31])
    roundoff=8.*math.ulp(1.);starts=[];ends=[];deviation=[];expected_terminal=[]
    for offset in (0,4,8):
        a,b,c,d=coefficients[offset:offset+4]
        i2=interval*interval;i3=i2*interval
        b_term=b*interval;c_term=c*i2;d_term=d*i3
        _require(all(math.isfinite(x) for x in (i2,i3,b_term,c_term,d_term)),
                 'type 112 polynomial exceeds finite evaluation range')
        scale=max(1.,abs(a),abs(b_term),abs(c_term),abs(d_term))
        # Bound the polynomial's distance from its endpoint chord over the full interval.
        residual=abs(c_term)/4.+abs(d_term)*(2./(3.*math.sqrt(3.)))
        _require(residual<=roundoff*scale,'type 112 polynomial is not a line at floating-point scale')
        finish=((d*interval+c)*interval+b)*interval+a
        derivative=(3.*d*interval+2.*c)*interval+b
        second=3.*d*interval+c
        _require(all(math.isfinite(x) for x in (finish,derivative,second)),
                 'type 112 terminal evaluation is not finite')
        starts.append(a);ends.append(finish);deviation.append(residual)
        expected_terminal.extend((finish,derivative,second,d))
    terminal_status=('consistent' if all(math.isclose(actual,expected,rel_tol=1e-10,abs_tol=1e-12)
                         for actual,expected in zip(terminal,expected_terminal)) else 'inconsistent')
    length=math.dist(starts,ends)
    _require(math.isfinite(length) and length>0.,'type 112 polynomial is degenerate')
    return {'status':'supported','kind':'line','length':length,'start':starts,'end':ends,
            'polynomial_domain_verified':True,
            'redundant_terminal_evaluations_status':terminal_status,
            'annotation':'Higher-order deviation from the endpoint chord is bounded at floating-point scale.'},deviation


def _cycle_equal(first, second):
    return any(first == second[i:]+second[:i] for i in range(len(second))) or any(
        first == list(reversed(second[i:]+second[:i])) for i in range(len(second)))


def verify_planar_quad(raw, corners, tolerance=1e-7):
    """Check the entire finite patch/trim definition against an ordered native loop.

    Unsupported semantic forms return a reason; malformed files raise ValueError.
    Plane deviation applies to the exported control net; corner/curve comparisons
    remain bounded by the supplied export-precision tolerance.
    """
    if (type(tolerance) not in (int,float) or not math.isfinite(tolerance) or tolerance<=0
            or type(corners) not in (list,tuple) or len(corners)!=4):
        raise ValueError('four corners and a finite positive tolerance are required')
    corners=[list(p) for p in corners]
    if any(len(p)!=3 or any(type(x) not in (int,float) or not math.isfinite(x) for x in p) for p in corners):
        raise ValueError('corners require three finite coordinates')
    parsed=iges._parse_iges(raw)
    try:
        allowed={102,112,126,128,142,144,308,314,406,408}
        _require(all(e.entity_type in allowed and not e.transform_pointer for e in parsed.entities),
                 'unsupported entity or transformation in FACE export')
        used=set()
        def single(kind):
            entities=[e for e in parsed.entities if e.entity_type==kind]
            _require(len(entities)==1, 'one IGES %d entity is required'%kind)
            return entities[0]
        def referenced(pointer, kind):
            key=iges._nonnegative_integer(pointer,'IGES pointer')
            entity=parsed.entities_by_sequence.get(key)
            _require(entity is not None and entity.entity_type==kind and entity.form==0,
                     'IGES reference kind/form mismatch')
            used.add(key)
            return entity
        trim=single(144);p=_params(trim,5);used.add(trim.sequence)
        _require(trim.form==0 and all(e.form==(15 if e.entity_type==406 else 0)
                 for e in parsed.entities if e.entity_type in (314,406)), 'unsupported trim or metadata form')
        _require(_integers(p[2:4])==(1,0),'one explicit outer trim and no inner loops required')
        surface=referenced(p[1],128);boundary=referenced(p[4],142)
        _require(surface==single(128) and boundary==single(142),'extra surface or trim linkage')
        s=_params(surface,38)
        _require(_integers(s[1:10])==(1,1,1,1,0,0,1,0,0),
                 'only open polynomial degree-one 2x2 surfaces supported')
        _require(_numbers(s[10:18])==(0.,0.,1.,1.,0.,0.,1.,1.) and _numbers(s[18:22])==(1.,)*4
                 and _numbers(s[34:38])==(0.,1.,0.,1.),'unit knots, weights and parameter domain required')
        coords=_numbers(s[22:34]);net=[list(coords[i:i+3]) for i in range(0,12,3)]
        loop=[net[0],net[1],net[3],net[2]]
        a,b,c=loop[:3]
        u=[b[i]-a[i] for i in range(3)];v=[c[i]-a[i] for i in range(3)]
        normal=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
        length=math.hypot(*normal);_require(length>tolerance*tolerance,'degenerate planar patch')
        normal=[x/length for x in normal]
        plane_error=max(abs(math.fsum((p[i]-a[i])*normal[i] for i in range(3))) for p in loop)
        _require(plane_error<=tolerance,'nonplanar control net')
        turns=[]
        for i in range(4):
            e=[loop[(i+1)%4][j]-loop[i][j] for j in range(3)]
            f=[loop[(i+2)%4][j]-loop[(i+1)%4][j] for j in range(3)]
            cross=[e[1]*f[2]-e[2]*f[1],e[2]*f[0]-e[0]*f[2],e[0]*f[1]-e[1]*f[0]]
            turns.append(math.fsum(cross[j]*normal[j] for j in range(3)))
        _require(all(t>tolerance*max(math.dist(loop[i],loop[(i+1)%4]) for i in range(4)) for t in turns),
                 'folded, concave or degenerate bilinear patch')
        q=_params(boundary,6)
        _require(_integers(q[1:3])==(0,surface.sequence) and _integers(q[5:6])[0] in (1,2,3),
                 'unsupported curve-on-surface definition or representation preference')
        def component_list(pointer, kind):
            composite=referenced(pointer,102);r=_params(composite,6)
            _require(_integers(r[1:2])==(4,),'four ordered trim components required')
            entities=[referenced(p,kind) for p in r[2:6]]
            _require(len({e.sequence for e in entities})==4,'duplicate trim component')
            return entities
        uv_curves=component_list(q[3],112);xyz_curves=component_list(q[4],126)
        uv=[];terminal=[];uv_deviation=[]
        for e in uv_curves:
            line,deviation=_type112_export_line(e);uv.append([line['start'],line['end']])
            terminal.append(line['redundant_terminal_evaluations_status'])
            uv_deviation.append(deviation)
        square=[[0.,0.,0.],[1.,0.,0.],[1.,1.,0.],[0.,1.,0.]]
        uv_vertices=[line[0] for line in uv]
        def uv_equal(first,second):
            return all(abs(a-b)<=8.*math.ulp(max(1.,abs(a),abs(b))) for a,b in zip(first,second))
        _require(all(uv_equal(uv[i][1],uv[(i+1)%4][0]) for i in range(4)),
                 'four ordered parameter-space trim lines must form one closed loop at export precision')
        for line,bound in zip(uv,uv_deviation):
            _require(line[0][2]==0. and line[1][2]==0. and bound[2]==0.,
                     'parameter-space trim must remain in the UV plane')
            for axis in (0,1):
                low=min(line[0][axis],line[1][axis]);high=max(line[0][axis],line[1][axis])
                _require(low-bound[axis]>=0. and high+bound[axis]<=1.,
                         'parameter-space trim must lie within the unit square')
        turns_uv=[]
        for i in range(4):
            p0=uv_vertices[i];p1=uv_vertices[(i+1)%4];p2=uv_vertices[(i+2)%4]
            turns_uv.append((p1[0]-p0[0])*(p2[1]-p1[1])-(p1[1]-p0[1])*(p2[0]-p1[0]))
        _require(len({tuple(point[:2]) for point in uv_vertices})==4
                 and (all(turn>0. for turn in turns_uv) or all(turn<0. for turn in turns_uv)),
                 'parameter-space trim must be a simple strictly convex quadrilateral')
        full_square=_cycle_equal(uv_vertices,square)
        if not full_square:
            # For a non-square trim, endpoint agreement proves full-segment
            # agreement only when the bilinear cross term vanishes at float scale.
            roundoff=8.*math.ulp(1.)
            for axis in range(3):
                values=[point[axis] for point in net]
                scale=max(abs(value) for value in values)
                cross=0. if scale==0. else math.fsum((values[3]/scale,-values[1]/scale,
                                                       -values[2]/scale,values[0]/scale))
                _require(abs(cross)<=roundoff,
                         'local trim requires an affine planar surface (nonzero bilinear cross term)')
        def evaluate(point):
            u,v,_=point
            weights=((1-u)*(1-v),u*(1-v),(1-u)*v,u*v)
            return [math.fsum(weights[n]*net[n][axis] for n in range(4)) for axis in range(3)]
        matching=[];maximum=0.
        for point in uv_vertices:
            mapped=evaluate(point)
            matches=[i for i,pnt in enumerate(corners) if math.dist(mapped,pnt)<=tolerance]
            _require(len(matches)==1,'mapped trim corner does not uniquely match native HOT POINT')
            matching.append(matches[0]);maximum=max(maximum,math.dist(mapped,corners[matches[0]]))
        _require(len(set(matching))==4 and _cycle_equal(matching,list(range(4))),
                 'mapped trim corner order differs from native CONS loop')
        for i in range(4):
            closure_error=math.dist(evaluate(uv[i][1]),evaluate(uv[(i+1)%4][0]))
            _require(closure_error<=tolerance,'mapped parameter-space trim is not closed within tolerance')
            maximum=max(maximum,closure_error)
        mapped_a=[net[1][i]-net[0][i] for i in range(3)]
        mapped_b=[net[2][i]-net[0][i] for i in range(3)]
        mapped_cross=[math.fsum((net[3][i],-net[1][i],-net[2][i],net[0][i])) for i in range(3)]
        mapped_segment_deviation=[]
        for i in range(4):
            start,end=uv[i]
            du=end[0]-start[0];dv=end[1]-start[1]
            eu,ev=uv_deviation[i][:2]
            bound=[abs(mapped_a[axis])*eu+abs(mapped_b[axis])*ev
                   +abs(mapped_cross[axis])*(eu+ev+eu*ev+abs(du*dv)/4.) for axis in range(3)]
            _require(all(math.isfinite(value) for value in bound),
                     'mapped parameter-curve deviation exceeds finite range')
            deviation_mm=math.hypot(*bound)
            _require(deviation_mm<=tolerance,
                     'parameter-space curve does not map to a model-space line within tolerance')
            mapped_segment_deviation.append(deviation_mm)
        # A full-square side varies along only one parameter axis, so even a
        # non-affine bilinear surface maps each such side to a straight segment.
        # A local trim passed the affine cross-term check above.
        for index,(curve,ends) in enumerate(zip(xyz_curves,uv)):
            r=_params(curve,24)
            _require(_integers(r[1:7])==(1,1,0,0,1,0),'only open degree-one model curves supported')
            _require(_numbers(r[7:13])==(0.,0.,1.,1.,1.,1.) and _numbers(r[19:24])==(0.,1.,0.,0.,0.),
                     'model curve requires unit knots, weights and parameter domain')
            values=_numbers(r[13:19]);endpoints=[values[:3],values[3:]]
            endpoint_errors=[]
            for actual,param in zip(endpoints,ends):
                error=math.dist(actual,evaluate(param));maximum=max(maximum,error)
                endpoint_errors.append(error)
            segment_error=max(endpoint_errors)+mapped_segment_deviation[index]
            _require(segment_error<=tolerance,'model-space trim disagrees with the full parameter-space segment')
            maximum=max(maximum,segment_error)
        geometry={e.sequence for e in parsed.entities if e.entity_type not in (314,406,308,408)}
        _require(geometry==used,'unexplained geometry in single-FACE export')
        iges._validate_identity_instance(trim,parsed)
        representation=('IGES128_bilinear_full_square_trim' if full_square else
                         'IGES128_affine_planar_local_convex_quad_trim')
        return {'status':'verified_at_export_precision','representation':representation,
                'cad_export_sha256':iges.canonical_digest(raw),'tolerance_mm':tolerance,
                'maximum_corner_or_trim_error_mm':maximum,'maximum_control_plane_error_mm':plane_error,
                'representation_preference':int(q[5]),'parameter_curve_terminal_metadata':terminal,
                'parameter_curve_linear_deviation_bound_uv':uv_deviation,
                'parameter_curve_mapped_deviation_bound_mm':mapped_segment_deviation,
                'continuous_export_domain_verified':True,'exact_native_CAD_equivalence_proven':False}
    except iges._Unsupported as exc:
        return {'status':'unsupported','reason':str(exc),'continuous_export_domain_verified':False,
                'exact_native_CAD_equivalence_proven':False}


def _capture(state, base, deck, witness, topology, cache):
    from . import geometry_features as features
    from .cad_sweep_topology import _trace_face_loop
    points={r['id']:r['position'] for r in witness['vertices']}
    boundaries={r['id']:{'endpoints':r['endpoint_topology']['hot_point_ids']} for r in witness['boundaries']}
    stats={'scoped_export_calls':0};rows=[]
    for face in witness['face_boundaries']:
        loop,_=_trace_face_loop(face['face_id'],face['cons_ids'],boundaries)
        corners=[points[i] for i in loop];tolerance=topology.get('tolerance',1e-7)
        key=(face['face_id'],tuple(loop),tuple(tuple(p) for p in corners),tolerance)
        if key not in cache:
            raw=features._export(state,base,[features._entity(base,deck,'FACE',face['face_id'])],stats)
            cache[key]=verify_planar_quad(raw,corners,tolerance)
        proof=copy.deepcopy(cache[key])
        rows.append({'face_id':face['face_id'],'hot_point_ids':loop,**proof})
    return {'status':'verified_at_export_precision' if all(r['continuous_export_domain_verified'] for r in rows) else 'unsupported',
            'faces':rows,'scoped_export_calls':stats['scoped_export_calls'],
            'source_snapshot_sha256':witness['source_snapshot_sha256'],
            'exact_native_CAD_equivalence_proven':False}


def capture(state, base, deck, witness, topology):
    """Freshly export the six native FACEs; never reuse a prior call's proof."""
    return _capture(state,base,deck,witness,topology,{})


def capture_many(state, base, deck, requests):
    """Share identical FACE proofs only inside this one main-thread read call."""
    from . import geometry_features as features
    state.assert_main()
    if type(requests) is not list or not 1<=len(requests)<=20:
        raise ValueError('capture_many requires 1..20 explicit witness/topology pairs')
    context=features._context(state,base,deck);cache={};result=[]
    for witness,topology in requests:
        result.append(_capture(state,base,deck,witness,topology,cache))
    if features._context(state,base,deck)!=context:
        raise RuntimeError('native model context changed during shared FACE proof capture')
    return result
