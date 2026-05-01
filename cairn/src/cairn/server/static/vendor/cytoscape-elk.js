(function webpackUniversalModuleDefinition(root, factory) {
	if(typeof exports === 'object' && typeof module === 'object')
		module.exports = factory(require("elkjs"));
	else if(typeof define === 'function' && define.amd)
		define(["elkjs"], factory);
	else if(typeof exports === 'object')
		exports["cytoscapeElk"] = factory(require("elkjs"));
	else
		root["cytoscapeElk"] = factory(root["ELK"]);
})(this, function(__WEBPACK_EXTERNAL_MODULE_4__) {
return /******/ (function(modules) { // webpackBootstrap
/******/ 	// The module cache
/******/ 	var installedModules = {};
/******/
/******/ 	// The require function
/******/ 	function __webpack_require__(moduleId) {
/******/
/******/ 		// Check if module is in cache
/******/ 		if(installedModules[moduleId]) {
/******/ 			return installedModules[moduleId].exports;
/******/ 		}
/******/ 		// Create a new module (and put it into the cache)
/******/ 		var module = installedModules[moduleId] = {
/******/ 			i: moduleId,
/******/ 			l: false,
/******/ 			exports: {}
/******/ 		};
/******/
/******/ 		// Execute the module function
/******/ 		modules[moduleId].call(module.exports, module, module.exports, __webpack_require__);
/******/
/******/ 		// Flag the module as loaded
/******/ 		module.l = true;
/******/
/******/ 		// Return the exports of the module
/******/ 		return module.exports;
/******/ 	}
/******/
/******/
/******/ 	// expose the modules object (__webpack_modules__)
/******/ 	__webpack_require__.m = modules;
/******/
/******/ 	// expose the module cache
/******/ 	__webpack_require__.c = installedModules;
/******/
/******/ 	// identity function for calling harmony imports with the correct context
/******/ 	__webpack_require__.i = function(value) { return value; };
/******/
/******/ 	// define getter function for harmony exports
/******/ 	__webpack_require__.d = function(exports, name, getter) {
/******/ 		if(!__webpack_require__.o(exports, name)) {
/******/ 			Object.defineProperty(exports, name, {
/******/ 				configurable: false,
/******/ 				enumerable: true,
/******/ 				get: getter
/******/ 			});
/******/ 		}
/******/ 	};
/******/
/******/ 	// getDefaultExport function for compatibility with non-harmony modules
/******/ 	__webpack_require__.n = function(module) {
/******/ 		var getter = module && module.__esModule ?
/******/ 			function getDefault() { return module['default']; } :
/******/ 			function getModuleExports() { return module; };
/******/ 		__webpack_require__.d(getter, 'a', getter);
/******/ 		return getter;
/******/ 	};
/******/
/******/ 	// Object.prototype.hasOwnProperty.call
/******/ 	__webpack_require__.o = function(object, property) { return Object.prototype.hasOwnProperty.call(object, property); };
/******/
/******/ 	// __webpack_public_path__
/******/ 	__webpack_require__.p = "";
/******/
/******/ 	// Load entry module and return exports
/******/ 	return __webpack_require__(__webpack_require__.s = 3);
/******/ })
/************************************************************************/
/******/ ([
/* 0 */
/***/ (function(module, exports, __webpack_require__) {

"use strict";


var ELK = __webpack_require__(4);
var assign = __webpack_require__(1);
var defaults = __webpack_require__(2);

var elkOverrides = {};

var getPos = function getPos(ele, options) {
  var dims = ele.layoutDimensions(options);
  var parent = ele.parent();
  var k = ele.scratch('elk');

  var p = {
    x: k.x,
    y: k.y
  };

  while (parent.nonempty()) {
    var kp = parent.scratch('klay');
    p.x += kp.x;
    p.y += kp.y;
    parent = parent.parent();
  }

  // elk considers a node position to be its top-left corner, while cy is the centre
  p.x += dims.w / 2;
  p.y += dims.h / 2;

  return p;
};

var makeNode = function makeNode(node, options) {
  var k = {
    _cyEle: node,
    id: node.id()
  };

  if (!node.isParent()) {
    var dims = node.layoutDimensions(options);
    var p = node.position();

    // the elk position is the top-left corner, cy is the centre
    k.x = p.x - dims.w / 2;
    k.y = p.y - dims.h / 2;

    k.width = dims.w;
    k.height = dims.h;
  }

  node.scratch('elk', k);

  return k;
};

var makeEdge = function makeEdge(edge /*, options*/) {
  var k = {
    _cyEle: edge,
    id: edge.id(),
    source: edge.data('source'),
    target: edge.data('target')
  };

  edge.scratch('elk', k);

  return k;
};

var makeGraph = function makeGraph(nodes, edges, options) {
  var elkNodes = [];
  var elkEdges = [];
  var elkEleLookup = {};
  var graph = {
    id: 'root',
    children: [],
    edges: []
  };

  // map all nodes
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    var k = makeNode(n, options);

    elkNodes.push(k);

    elkEleLookup[n.id()] = k;
  }

  // map all edges
  for (var _i = 0; _i < edges.length; _i++) {
    var e = edges[_i];
    var _k = makeEdge(e, options);

    elkEdges.push(_k);

    elkEleLookup[e.id()] = _k;
  }

  // make hierarchy
  for (var _i2 = 0; _i2 < elkNodes.length; _i2++) {
    var _k2 = elkNodes[_i2];
    var _n = _k2._cyEle;

    if (!_n.isChild()) {
      graph.children.push(_k2);
    } else {
      var parent = _n.parent();
      var parentK = elkEleLookup[parent.id()];

      var children = parentK.children = parentK.children || [];

      children.push(_k2);
    }
  }

  for (var _i3 = 0; _i3 < elkEdges.length; _i3++) {
    var _k3 = elkEdges[_i3];

    // put all edges in the top level for now
    // TODO does this cause issues in certain edgecases?
    /*let e = k._cyEle;
    let parentSrc = e.source().parent();
    let parentTgt = e.target().parent();
    if ( false && parentSrc.nonempty() && parentTgt.nonempty() && parentSrc.same( parentTgt ) ){
      let kp = elkEleLookup[ parentSrc.id() ];
       kp.edges = kp.edges || [];
       kp.edges.push( k );
    } else {*/
    graph.edges.push(_k3);
    //}
  }

  return graph;
};

function Layout(options) {
  var elkOptions = options.elk;
  var cy = options.cy;

  this.options = assign({}, defaults, options);

  this.options.elk = assign({
    aspectRatio: cy.width() / cy.height()
  }, defaults.elk, elkOptions, elkOverrides);
}

Layout.prototype.run = function () {
  var layout = this;
  var options = this.options;

  var eles = options.eles;
  var nodes = eles.nodes();
  var edges = eles.edges();

  var elk = new ELK();
  var graph = makeGraph(nodes, edges, options);

  elk.layout(graph, {
    layoutOptions: options.elk
  }).then(function () {
    nodes.filter(function (n) {
      return !n.isParent();
    }).layoutPositions(layout, options, function (n) {
      return getPos(n, options);
    });
  });

  return this;
};

Layout.prototype.stop = function () {
  return this; // chaining
};

Layout.prototype.destroy = function () {
  return this; // chaining
};

module.exports = Layout;

/***/ }),
/* 1 */
/***/ (function(module, exports, __webpack_require__) {

"use strict";


// Simple, internal Object.assign() polyfill for options objects etc.

module.exports = Object.assign != null ? Object.assign.bind(Object) : function (tgt) {
  for (var _len = arguments.length, srcs = Array(_len > 1 ? _len - 1 : 0), _key = 1; _key < _len; _key++) {
    srcs[_key - 1] = arguments[_key];
  }

  srcs.forEach(function (src) {
    Object.keys(src).forEach(function (k) {
      return tgt[k] = src[k];
    });
  });

  return tgt;
};

/***/ }),
/* 2 */
/***/ (function(module, exports, __webpack_require__) {

"use strict";


var defaults = {
  nodeDimensionsIncludeLabels: false, // Boolean which changes whether label dimensions are included when calculating node dimensions
  fit: true, // Whether to fit
  padding: 20, // Padding on fit
  animate: false, // Whether to transition the node positions
  animateFilter: function animateFilter() {
    return true;
  }, // Whether to animate specific nodes when animation is on; non-animated nodes immediately go to their final positions
  animationDuration: 500, // Duration of animation in ms if enabled
  animationEasing: undefined, // Easing of animation if enabled
  transform: function transform(node, pos) {
    return pos;
  }, // A function that applies a transform to the final node position
  ready: undefined, // Callback on layoutready
  stop: undefined, // Callback on layoutstop
  elk: { // Options to pass directly to ELK `layoutOptions`
    // the elk algorithm to use
    // one of 'box', 'disco', 'force', 'layered', 'mrtree', 'radial', 'random', 'stress'
    // (see https://www.eclipse.org/elk/reference/algorithms.html)
    algorithm: undefined
  },
  priority: function priority() {
    return null;
  } // Edges with a non-nil value are skipped when geedy edge cycle breaking is enabled
};

module.exports = defaults;

/***/ }),
/* 3 */
/***/ (function(module, exports, __webpack_require__) {

"use strict";


var impl = __webpack_require__(0);

// registers the extension on a cytoscape lib ref
var register = function register(cytoscape) {
  if (!cytoscape) {
    return;
  } // can't register if cytoscape unspecified

  cytoscape('layout', 'elk', impl); // register with cytoscape.js
};

if (typeof cytoscape !== 'undefined') {
  // expose to global cytoscape (i.e. window.cytoscape)
  // eslint-disable-next-line no-undef
  register(cytoscape);
}

module.exports = register;

/***/ }),
/* 4 */
/***/ (function(module, exports) {

module.exports = __WEBPACK_EXTERNAL_MODULE_4__;

/***/ })
/******/ ]);
});