west will automatically reconfigure and rebuild if
* prj.conf is meaningfully hanged
* sysbuild.conf is meaningfully changed
* app.overlay is changed

What needs a pristine build
* pm.static is changed
* No CMakeCache / build.ninja in build dir (broken build dir)
* Any changes to the nordic sdk (version change/reinstall)