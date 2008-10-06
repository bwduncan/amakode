

dist:
	rm -rf dist build
	mkdir -p dist build/amakode
	cp src/amakode.spec src/amakode.py src/README build/amakode/
	tar -zcf dist/amakode.amarokscript.tar.gz -C build amakode
	rm -rf build

clean:
	rm -f *~ src/*~
	rm -rf dist build

.PHONY: dist clean
