export function duration(clips){return clips.reduce((sum,c)=>sum+c.end-c.start,0)}
export function sourceToEdit(clips,t){let at=0;for(const c of clips){if(t<c.start)return at;if(t<=c.end)return at+t-c.start;at+=c.end-c.start}return at}
export function editToSource(clips,t){let at=0;for(const c of clips){if(t<=at+c.end-c.start)return c.start+Math.max(0,t-at);at+=c.end-c.start}return clips.at(-1)?.end||0}
export function trimClip(clips,index,edge,delta,fps,sourceDuration){const c=clips[index],frame=1/fps,round=t=>Math.round(t/frame)*frame;if(edge==='left')c.start=Math.max(index?clips[index-1].end:0,Math.min(c.end-frame,round(c.start+delta)));else c.end=Math.min(index+1<clips.length?clips[index+1].start:sourceDuration,Math.max(c.start+frame,round(c.end+delta)));return c}
export function initialMedia(project){
  const rendered=Boolean(project.preview_file&&project.preview_revision===project.revision);
  return {rendered,src:`/media/${project.id}/${rendered?project.preview_file:'source.mp4'}`};
}
