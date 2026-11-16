"""Canonical rule parameter schema shared by API and event-service."""
def number(key, label, low, high, default, integer=False):
    return dict(key=key, label=label, min=low, max=high, default=default, type='integer' if integer else 'number')

PARAMETERS = {
    'crowd_gathering': [number('count_threshold','聚集人数',2,1000,3,True), number('distance_threshold','聚集归一化距离',.01,.5,.18)],
    'seat_absence': [number('absence_seconds','连续离席秒数',1,86400,60)],
    'zone_intrusion': [number('person_threshold','限制区人数',1,100,1,True)],
    'phone_call': [number('duration_seconds','通话姿态持续秒数',1,30,2)], 'phone_capture': [number('duration_seconds','拍摄姿态持续秒数',.5,30,1)], 'phone_unstored': [], 'phone_detected': [],
    'whispering': [number('duration_seconds','近距交流持续秒数',1,30,3)],
    'workstation_visit': [number('dwell_seconds','他人工位停留秒数',1,600,5)],
    'document_transfer': [number('duration_seconds','递接过程最短秒数',.1,10,.5)],
    'frequent_absence': [number('window_seconds','统计窗口秒数',30,3600,600), number('count_threshold','离席次数',2,30,3,True)],
    'joint_absence': [number('departure_window_seconds','共同离席时间窗秒数',1,60,10), number('absence_seconds','离席确认秒数',1,600,5), number('count_threshold','共同离席工位数',2,100,2,True)],
    'unauthorized_entry': [],
}
